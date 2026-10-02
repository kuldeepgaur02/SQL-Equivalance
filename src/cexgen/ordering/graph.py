"""The FK graph: tables are nodes, a foreign key is an edge child -> parent.

Cycles are found as strongly connected components (Tarjan, iterative so deep
graphs cannot overflow the stack). Every ordering breaks ties by table name,
so the same schema always gives the same plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..schema.model import ForeignKey
from ..schema.names import QName


@dataclass(frozen=True)
class Edge:
    child: QName
    parent: QName                 # the table that gets the parent row (a partition's root, not the partition)
    fk: ForeignKey


class FkGraph:
    def __init__(self, tables: Iterable[QName], edges: Iterable[Edge]):
        self.tables = sorted(set(tables))
        live = set(self.tables)
        self.edges = sorted((e for e in edges if e.child in live and e.parent in live and e.child != e.parent),
                            key=lambda e: (e.child, e.fk.name))

    def parents(self, table: QName) -> list[QName]:
        return sorted({e.parent for e in self.edges if e.child == table})

    def components(self) -> list[list[QName]]:
        """Strongly connected components; a component of 2+ tables is a cycle."""
        return strongly_connected(self.tables, {t: self.parents(t) for t in self.tables})

    def ordered_components(self) -> list[list[QName]]:
        """Components with every parent component before its children."""
        comps = self.components()
        comp_of = {t: i for i, c in enumerate(comps) for t in c}
        needs: dict[int, set[int]] = {i: set() for i in range(len(comps))}
        for e in self.edges:
            a, b = comp_of[e.child], comp_of[e.parent]
            if a != b:
                needs[a].add(b)
        done: set[int] = set()
        out = []
        while len(out) < len(comps):
            ready = sorted((i for i in needs if i not in done and needs[i] <= done), key=lambda i: min(comps[i]))
            i = ready[0]                           # a DAG of components always has a ready one
            done.add(i)
            out.append(sorted(comps[i]))
        return out


def strongly_connected(nodes: list, successors: dict) -> list[list]:
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    out: list[list] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(successors[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(successors[w])))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out
