"""Capture addresses: expanded zero-based vector IDs, or a STIL line plus an explicit occurrence.

A source line inside a Loop (or repeated through a Macro/Procedure) maps to
several vectors; such a line is rejected unless the occurrence is given.
Output-event indices, sample indices, STATUS progress and times are not
accepted as addresses.
"""

from __future__ import annotations

from dataclasses import dataclass

from stpg_runner.artifacts import RunnerError

from .timeline import BundleTimeline, SegmentTimeline


class AddressError(RunnerError):
    pass


@dataclass(frozen=True)
class Address:
    vector: int | None = None
    line: int | None = None
    occurrence: int | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "Address":
        if not isinstance(value, dict) or set(value) - {"vector", "line", "occurrence"}:
            raise AddressError("address takes only vector, or line with an optional occurrence")
        for key in ("vector", "line", "occurrence"):
            item = value.get(key)
            if item is not None and (type(item) is not int or item < 0):
                raise AddressError(f"address.{key} must be a non-negative integer")
        vector, line, occurrence = value.get("vector"), value.get("line"), value.get("occurrence")
        if (vector is None) == (line is None):
            raise AddressError("give exactly one of address.vector or address.line")
        if vector is not None and occurrence is not None:
            raise AddressError("address.occurrence applies to a STIL line only")
        return cls(vector, line, occurrence)

    def to_dict(self) -> dict:
        if self.vector is not None:
            return {"vector": self.vector}
        out: dict = {"line": self.line}
        if self.occurrence is not None:
            out["occurrence"] = self.occurrence
        return out


@dataclass(frozen=True)
class ResolvedAddress:
    vector: int  # global expanded vector ID
    segment: int
    local_vector: int
    line: int | None
    occurrence: int | None
    occurrences: int | None  # how many vectors the source line produced (line addresses only)

    def to_dict(self) -> dict:
        return {"vector": self.vector, "segment": self.segment, "local_vector": self.local_vector,
                "line": self.line, "occurrence": self.occurrence, "occurrences": self.occurrences}


def line_occurrences(timeline: BundleTimeline, line: int) -> list[int]:
    found = []
    for segment in timeline.segments:
        lines = segment.trace.lines
        found.extend(segment.global_start + i for i, value in enumerate(lines) if value == line)
    return found


def resolve(timeline: BundleTimeline, address: Address) -> ResolvedAddress:
    if address.vector is not None:
        segment, local = timeline.locate(address.vector)
        line = segment.trace.lines[local]
        return ResolvedAddress(address.vector, segment.index, local, None if line < 0 else line, None, None)
    vectors = line_occurrences(timeline, address.line)
    if not vectors:
        raise AddressError(f"STIL line {address.line} produced no vector in this bundle "
                           "(no map, or the line is not a vector statement)")
    if address.occurrence is None:
        if len(vectors) > 1:
            raise AddressError(f"STIL line {address.line} produced {len(vectors)} vectors (Loop/Macro/repeat); "
                               f"give occurrence 0..{len(vectors) - 1}")
        occurrence = 0
    else:
        occurrence = address.occurrence
        if occurrence >= len(vectors):
            raise AddressError(f"STIL line {address.line} has {len(vectors)} occurrence(s); "
                               f"occurrence {occurrence} does not exist")
    vector = vectors[occurrence]
    segment, local = timeline.locate(vector)
    return ResolvedAddress(vector, segment.index, local, address.line, occurrence, len(vectors))


def window(segment: SegmentTimeline, local_center: int, before: int, after: int) -> tuple[int, int, list[str]]:
    """Clip [center-before, center+after] to the segment; captures never cross a segment's RUN."""

    notes = []
    first, last = local_center - before, local_center + after
    if first < 0:
        notes.append(f"window starts {-first} vector(s) before segment {segment.index}; clipped "
                     "(a capture cannot span two RUN commands)")
        first = 0
    if last >= segment.vector_count:
        notes.append(f"window ends {last - segment.vector_count + 1} vector(s) after segment {segment.index}; "
                     "clipped")
        last = segment.vector_count - 1
    return first, last, notes
