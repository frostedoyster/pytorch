# mypy: allow-untyped-defs
# pyre-strict
from __future__ import annotations

import heapq

import sys
from collections import defaultdict
from typing import Dict, List, Set, TYPE_CHECKING

import torch
from torch import _inductor

from . import config, ir
from .dependencies import WeakDep
from .utils import contains_collective, contains_wait

overlap_log = torch._logging.getArtifactLogger(__name__, "overlap")
import logging

torch_log = logging.getLogger("torch")

if TYPE_CHECKING:
    from .scheduler import BaseSchedulerNode


def sink_waits(snodes: List[BaseSchedulerNode]) -> List[BaseSchedulerNode]:
    """
    Greedily schedules waits as late as possible.
    """
    return _schedule_for_comm(
        snodes, raise_comms=False, sink_waits=True, reorder_for_overlap=False
    )


def raise_comms(snodes: List[BaseSchedulerNode]) -> List[BaseSchedulerNode]:
    """
    Greedily schedules comms as early as possible.
    """
    return _schedule_for_comm(
        snodes, raise_comms=True, sink_waits=False, reorder_for_overlap=False
    )


def reorder_compute_for_overlap(
    snodes: List[BaseSchedulerNode],
) -> List[BaseSchedulerNode]:
    """
    This achieves the following overall scheduling procedure:
        Step 1: Given that we've currently scheduled comm N, we now schedule all compute nodes
            that are required for comm N + 1 but do not depend on comm N, to run at the same time with comm N.
        Step 2: If all those compute nodes are sufficient to overlap comm N, we're done.
            Otherwise, we now need to look elsewhere to find compute that overlaps with comm N.
            We prioritize compute nodes that are needed sooner.
        Step 3: We schedule the compute nodes dependent on comm N and required for comm N + 1.
        Step 4: We schedule comm N + 1.
        Repeat this for subsequent comm nodes.
    """
    return _schedule_for_comm(
        snodes, raise_comms=True, sink_waits=True, reorder_for_overlap=True
    )


def _schedule_for_comm(
    snodes: List[BaseSchedulerNode],
    raise_comms: bool,
    sink_waits: bool,
    reorder_for_overlap: bool,
) -> List[BaseSchedulerNode]:
    """
    Schedule `snodes` for various comm optimization objectives.

    Args:
        snodes: the nodes to be scheduled.
        raise_comms: whether to greedily schedule collectives as early as possible
        sink_wait: whether to greedily schedule waits as late as possible
        reorder_compute_for_overlap: whether to reorder compute nodes to
            optimize for compute/communication overlapping.

    Returns:
        The new schedule order.

    Some notes on the synergy between different options:
        - `raise_comms` provides more overlapping oppurtunies for `reorder_compute_for_overlap`.
        - When both `raise_comms` and `sink_waits` is `True`, `raise_comms` is prioritized.
    """
    # We assign each node a tuple of scores (score_0, score_1, score_2),
    # decreasing in importance, with a lower value indicating a higher ranking:
    #
    # - score_0: the lowest comm_idx among the comm nodes that the node blocks.
    # If a node doesn't block any comm nodes, its score_0 is set to
    # sys.maxsize. This score ensures that comm nodes get scheduled as early as
    # possible.
    # - score_1: 1 if the node is a wait node, 0 otherwise. This score ensures
    # that wait nodes are deferred as late as possible.
    # - score_2: the index of the node in the original topological order. This
    # score provides stability in case of ties.
    #
    # When only raise_comms is True, only score_0 and score_2 are considered.
    # When only sink_waits is True, only score_1 and score_2 are considered.
    # When neither is True, the original order is yielded.
    name_to_snode = {}
    scores_0, scores_1, scores_2 = {}, {}, {}
    for idx, snode in enumerate(snodes):
        if isinstance(snode, _inductor.scheduler.GroupedSchedulerNode):
            name = snode.get_name()
            name_to_snode[name] = snode
            scores_0[name] = sys.maxsize
            scores_1[name] = 0
            scores_2[name] = idx
        else:
            for name in snode.get_names():
                name_to_snode[name] = snode
                scores_0[name] = sys.maxsize
                scores_1[name] = 0
                scores_2[name] = idx

    comm_idx = 0
    for snode in snodes:
        if raise_comms and contains_collective(snode):
            scores_0[snode.get_name()] = comm_idx
            for anc in snode.ancestors:
                scores_0[anc] = min(scores_0[anc], comm_idx)
            comm_idx += 1
        elif sink_waits and contains_wait(snode):
            scores_1[snode.get_name()] = 1

    class Runnable:
        def __init__(self, snode):
            self.snode = snode
            name = next(iter(snode.get_names()))
            self.score = (
                scores_0[name],
                scores_1[name],
                scores_2[name],
            )

        def __lt__(self, other):
            return self.score < other.score

    # A mutating node's unmet_dependencies doesn't cover the dependencies
    # caused by the mutation. Instead, they are described by associated
    # MutationOutput node. Thus, to safely schedule a mutating node, we have to
    # add the unmet_dependencies of the associated MutationOutput nodes to the
    # mutating node.
    # TODO(yifu): this is needed due to a mutation handling bug in the
    # scheduler. It should be fixed by https://github.com/pytorch/pytorch/pull/128893.
    # We can remove this logic once the fix is landed.
    unmet_deps: Dict[BaseSchedulerNode, Set[str]] = {}
    for snode in snodes:
        if isinstance(snode.node, ir.MutationOutput):
            src_name = snode.node.node_doing_mutating.get_name()
            src_snode = name_to_snode[src_name]
            assert src_snode in unmet_deps
            unmet_deps[src_snode] |= {
                dep.name for dep in snode.unmet_dependencies if dep.name != src_name
            }
        assert snode not in unmet_deps
        unmet_deps[snode] = {dep.name for dep in snode.unmet_dependencies}
    for snode in unmet_deps:
        if isinstance(snode, _inductor.scheduler.GroupedSchedulerNode):
            for dep_name in list(unmet_deps[snode]):
                if isinstance(name_to_snode[dep_name], _inductor.scheduler.NopKernelSchedulerNode) and isinstance(name_to_snode[dep_name].node, ir.MutationOutput):
                    unmet_deps[snode].remove(dep_name)
        # deps = list(unmet_deps[snode])
        # if len(deps) == 1:
        #     print(f"deps: {deps}")
        #     if isinstance(name_to_snode[deps[0]], _inductor.scheduler.NopKernelSchedulerNode) and isinstance(name_to_snode[deps[0]].node, ir.MutationOutput):
        #         nop_node = name_to_snode[deps[0]]
        #         nop_node_deps = list(unmet_deps[nop_node])
        #         print(f"nop_node_deps[0]: {nop_node_deps[0]}")
        #         if len(nop_node_deps) == 1 and nop_node_deps[0] in snode.get_names():
        #             # unbreak the cycle between grouped node and its associated mutation output node
        #             unmet_deps[snode] = set()

    ready: List[Runnable] = []
    buffer_users: Dict[str, Set[BaseSchedulerNode]] = defaultdict(set)
    snode_to_cost = {snode: estimate_op_runtime(snode) for snode in snodes}

    for snode, deps in unmet_deps.items():
        if len(deps) == 0:
            heapq.heappush(ready, Runnable(snode))
        for dep in deps:
            buffer_users[dep].add(snode)

    scheduled = []

    def schedule(snode):
        """
        Schedules `snode` and put all unblocked nodes onto the ready queue.
        """
        scheduled.append(snode)
        for buf_name in snode.get_names():
            for snode in buffer_users[buf_name]:
                unmet_deps[snode].remove(buf_name)
                if len(unmet_deps[snode]) == 0:
                    heapq.heappush(ready, Runnable(snode))

    def get_overlapping_candidate():
        """
        Return the next node in the ready queue that's neither a collective or
        a wait.
        """
        candidates = [
            x
            for x in ready
            if not contains_collective(x.snode) and not contains_wait(x.snode)
        ]
        if len(candidates) == 0:
            return None
        return min(candidates, key=lambda x: x.score)

    def schedule_collective_for_overlap(snode):
        """
        Schedules collective node `snode`, along with one or more compute nodes
        to overlap with it. The strategy is described in the comment of
        `reorder_compute_for_overlap`.
        """
        assert contains_collective(snode)
        schedule(snode)

        collective_cost = snode_to_cost[snode]
        while (
            collective_cost > 0
            and (candidate := get_overlapping_candidate()) is not None
        ):
            ready.remove(candidate)
            schedule(candidate.snode)
            collective_cost -= snode_to_cost[candidate.snode]
        heapq.heapify(ready)

    while len(ready):
        snode = heapq.heappop(ready).snode
        if reorder_for_overlap and contains_collective(snode):
            schedule_collective_for_overlap(snode)
        else:
            schedule(snode)

    for snode, deps in unmet_deps.items():
        if len(deps) == 1:
            dep_node = name_to_snode[list(deps)[0]]
            print(f"dep snode: {dep_node}, dep_node.debug_str(): {dep_node.debug_str()}, unmet_deps[dep_node]: {unmet_deps[dep_node]}" )
        assert len(deps) == 0, (
            "Detected unscheduled nodes. "
            f"Nodes with unmet dependencies: {snode}, deps: {deps}"
        )
    return scheduled


def decide_global_ordering_of_comms(snodes: List[BaseSchedulerNode]):
    """
    Decide global ordering of comms, by just enforcing the ordering that's in the input graph
    (might not be the same ordering as the eager mode program).
    TODO: Come up with a better approach
    """
    comm_snodes = [sn for sn in snodes if contains_collective(sn)]
    for i in range(1, len(comm_snodes)):
        # Enforce ordering by making previous comm a `WeakDep` dependency of the next comm
        comm_snodes[i].add_fake_dep(WeakDep(comm_snodes[i - 1].get_name()))


def estimate_op_runtime(snode: BaseSchedulerNode) -> float:
    """
    Returns estimated op runtime in nanoseconds (ns)
    """
    if config.estimate_op_runtime == "default":
        runtime = snode.get_estimated_runtime()
    else:
        assert callable(config.estimate_op_runtime)
        runtime = config.estimate_op_runtime(snode)
    return runtime


def node_summary(snode):
    detail = ""
    if isinstance(snode.node, ir.ExternKernelOut):
        detail = f" ({snode.node.python_kernel_name})"
    out_tensor_info = ""
    if (
        hasattr(snode.node, "layout")
        and hasattr(snode.node.layout, "size")
        and hasattr(snode.node.layout, "stride")
    ):
        out_tensor_info = (
            f" (size={snode.node.layout.size}, stride={snode.node.layout.stride})"
        )
    node_name = ""
    if hasattr(snode.node, "name"):
        node_name = snode.node.name
    return f"{snode.node.__class__.__name__}{detail}{out_tensor_info} ({node_name})"


def visualize_overlap(order):
    total_est_runtime: float = 0.0
    cur_comm_node = None
    for snode in order:
        if cur_comm_node is None:
            if contains_collective(snode):
                total_est_runtime += estimate_op_runtime(snode)
                cur_comm_node = snode.node
            elif contains_wait(snode):
                raise AssertionError(
                    "Wait is not expected when there is no collective running"
                )
            else:  # exposed compute op
                total_est_runtime += estimate_op_runtime(snode)
            overlap_log.debug(f"{node_summary(snode)}")  # noqa: G004
        else:  # cur_comm_node is not None
            if contains_collective(snode):
                raise AssertionError(
                    "Found two collectives running at the same time. "
                    "`visualize_overlap` needs to be updated to handle this case"
                )
            elif contains_wait(snode):  # end of this comm op
                overlap_log.debug(f"{node_summary(snode)}")  # noqa: G004
                cur_comm_node = None
            else:  # overlapped compute op
                overlap_log.debug(f"| {node_summary(snode)}")  # noqa: G004
    overlap_log.debug(
        f"Est. runtime (ms): {total_est_runtime / 1000 / 1000}"  # noqa: G004
    )


def reorder_compute_and_comm_for_overlap(
    snodes: List[BaseSchedulerNode],
) -> List[BaseSchedulerNode]:
    order = snodes

    for p in config.reorder_for_compute_comm_overlap_passes:
        if isinstance(p, str) and p in globals():
            p = globals()[p]  # it is a builtin pass
        if torch.distributed.get_rank() == 0:
            overlap_log.debug(
                f"==== Visualize overlap before reordering pass {p} ===="  # noqa: G004
            )
            try:
                visualize_overlap(order)
            except Exception as e:
                overlap_log.debug(str(e))
        order = p(order)  # type: ignore[operator]
        if torch.distributed.get_rank() == 0:
            overlap_log.debug(
                f"==== Visualize overlap after reordering pass {p} ===="  # noqa: G004
            )
            try:
                visualize_overlap(order)
            except Exception as e:
                overlap_log.debug(str(e))
    return order


def get_all_reads(snode):
    reads = set()
    reads.update(snode.read_writes.reads)
    if isinstance(
        snode,
        (
            _inductor.scheduler.FusedSchedulerNode,
            _inductor.scheduler.GroupedSchedulerNode,
        ),
    ):
        for sub_snode in snode.snodes:
            reads.update(get_all_reads(sub_snode))
    return reads


def is_fallback_op(node, op):
    return isinstance(node, ir.FallbackKernel) and node.op_overload is op


def enforce_comm_ordering_for_fsdp(
    snodes: List[_inductor.scheduler.BaseSchedulerNode],
    **kwargs,
) -> List[_inductor.scheduler.BaseSchedulerNode]:
    from . import scheduler

    name_to_fused_node = kwargs["name_to_fused_node"]  # op name to (maybe fused) op
    graph_inputs = kwargs["graph_inputs"]
    # name_to_buf = kwargs["name_to_buf"]

    # def buf_name_to_snode(buf_name):
    #     return name_to_buf[buf_name].defining_op

    def _find_all_recursive_deps_of_node_up_to_criteria(
        snode, collected_node_set, criteria_cb=None
    ):
        collected_node_set.add(snode)
        if criteria_cb and criteria_cb(snode):
            return
        for dep in snode.unmet_dependencies:
            # dep_node = name_to_fused_node[buf_name_to_snode(dep.name).get_name()]
            dep_node = name_to_fused_node[dep.name]
            if dep_node in collected_node_set:
                continue
            _find_all_recursive_deps_of_node_up_to_criteria(
                dep_node, collected_node_set, criteria_cb
            )

    new_order: list[BaseSchedulerNode] = []
    scheduled = set()
    ag_nodes = []
    rs_nodes = []
    ag_wait_op_to_ag_related_ops = defaultdict(set)
    snode_name_to_final_snode = {}

    def _create_group_node(snodes_to_group):
        group_node = scheduler.GroupedSchedulerNode.create(snodes_to_group)
        for snode in snodes_to_group:
            snode_name_to_final_snode[snode.get_name()] = group_node
        return group_node

    # Create grouped nodes for specific ops
    for snode in snodes:
        if (
            isinstance(snode.node, ir.SetSourceTensorKernel)
            and any(
                is_fallback_op(
                    name_to_fused_node[x].node,
                    op=torch.ops.fsdp.split_with_sizes_copy.default,
                )
                for x in snode.ancestors
            )
        ):
            # Case 1: Handle AllGather

            # Find the "cast + copy_in + getitem + all_gather + all_gather_wait_tensor + copy_out + set_" code block
            collected_node_set: set[scheduler.BaseSchedulerNode] = set()
            _find_all_recursive_deps_of_node_up_to_criteria(
                snode,
                collected_node_set,
            )

            # Multiple .set_ nodes could recursively go up to the same all_gather op,
            # so we use a set in `ag_wait_op_to_ag_related_ops` to deduplicate.
            wait_node = None
            for n in collected_node_set:
                if isinstance(n.node, ir._WaitKernel):
                    wait_node = n
                    break
            ag_wait_op_to_ag_related_ops[wait_node].update(collected_node_set)
        elif (
            isinstance(snode.node, ir._WaitKernel)
            and any(
                is_fallback_op(
                    name_to_fused_node[x].node, torch.ops.fsdp.chunk_cat.default
                )
                for x in snode.ancestors
            )
        ):
            # Case 2: Handle ReduceScatter

            # Find the "reduce_scatter copy-in + reduce_scatter comm + reduce_scatter wait" code block
            collected_node_set = set()
            _find_all_recursive_deps_of_node_up_to_criteria(
                snode,
                collected_node_set,
                criteria_cb=lambda snode: is_fallback_op(
                    snode.node, torch.ops.fsdp.chunk_cat.default
                ),
            )
            # sort nodes by original operation order
            collected_nodes = sorted(
                collected_node_set, key=lambda x: int(x.get_name()[3:])
            )

            # Group "reduce_scatter copy-in + reduce_scatter comm" into one GroupedSchedulerNode
            wait_node_idx = None
            for i in range(len(collected_nodes) - 1):
                if isinstance(collected_nodes[i + 1].node, ir._WaitKernel):
                    wait_node_idx = i + 1
                    break
            assert wait_node_idx is not None
            rs_group_node = _create_group_node(collected_nodes[:wait_node_idx])

            # Group "reduce_scatter wait + related output nodes" into one GroupedSchedulerNode
            wait_group_node = _create_group_node(collected_nodes[wait_node_idx:])

            rs_nodes.append(
                (
                    rs_group_node,
                    wait_group_node,
                )
            )

    for ag_wait_node, collected_node_set in ag_wait_op_to_ag_related_ops.items():
        # sort nodes by original operation order
        collected_nodes = sorted(
            collected_node_set, key=lambda x: int(x.get_name()[3:])
        )
        wait_node_idx = collected_nodes.index(ag_wait_node)
        # Group "cast + copy_in + getitem + all_gather" into one GroupedSchedulerNode
        ag_group_node = _create_group_node(collected_nodes[:wait_node_idx])
        # Group "all_gather_wait_tensor + copy_out + set_" into one GroupedSchedulerNode
        wait_group_node = _create_group_node(collected_nodes[wait_node_idx:])
        ag_nodes.append(
            (
                ag_group_node,
                wait_group_node,
            )
        )

    for snode in snodes:
        if snode.get_name() in snode_name_to_final_snode:
            snode = snode_name_to_final_snode[snode.get_name()]
        if snode in scheduled:
            continue
        new_order.append(snode)
        scheduled.add(snode)

    # Enforce AllGather ordering: previous AllGather's "wait then copy_out" group node must run
    # before next AllGather's "copy_in then AG" group node
    prev_ag_wait = None
    for ag_group_node, wait_group_node in ag_nodes:
        if prev_ag_wait is not None:
            ag_group_node.add_fake_dep(WeakDep(prev_ag_wait.get_name()))
        prev_ag_wait = wait_group_node

    # Enforce ReduceScatter ordering: previous ReduceScatter's "wait" group node must run
    # before next ReduceScatter's "copy_in then RS" group node
    prev_rs_wait = None
    for rs_group_node, wait_group_node in rs_nodes:
        if prev_rs_wait is not None:
            rs_group_node.add_fake_dep(WeakDep(prev_rs_wait.get_name()))
        prev_rs_wait = wait_group_node

    return new_order  # type: ignore[return-value]
