from cexgen.ordering import Edge, FkGraph, strongly_connected
from cexgen.schema import ForeignKey, QName


def q(name):
    return QName("public", name)


def edge(child, parent):
    return Edge(q(child), q(parent), ForeignKey(f"{child}_{parent}", q(child), ("x",), q(parent), ("id",)))


def test_parents_come_first_and_ties_are_stable():
    g = FkGraph([q("c"), q("b"), q("a"), q("z")], [edge("c", "b"), edge("b", "a"), edge("z", "a")])
    assert g.ordered_components() == [[q("a")], [q("b")], [q("c")], [q("z")]]


def test_cycles_are_components():
    g = FkGraph([q(n) for n in "abcd"], [edge("a", "b"), edge("b", "c"), edge("c", "a"), edge("d", "a")])
    comps = g.ordered_components()
    assert comps[0] == [q("a"), q("b"), q("c")] and comps[1] == [q("d")]


def test_self_loops_and_unknown_tables_are_ignored():
    g = FkGraph([q("a")], [edge("a", "a"), edge("a", "missing")])
    assert g.edges == [] and g.ordered_components() == [[q("a")]]


def test_deep_chain_does_not_overflow():
    n = 5000
    succ = {i: [i + 1] if i + 1 < n else [] for i in range(n)}
    assert len(strongly_connected(list(range(n)), succ)) == n
