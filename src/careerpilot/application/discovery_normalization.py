"""Pure bounded normalization/deduplication; no LangChain, network or storage."""

from careerpilot.application.discovery_contracts import (
    DeduplicatedJobs,
    DiscoveryConflict,
    JobGroup,
    SearchBatch,
    SourceReference,
)


def deduplicate(batch: SearchBatch) -> DeduplicatedJobs:
    hits = batch.hits
    parent = list(range(len(hits)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    seen: dict[tuple[str, ...], int] = {}
    identities: dict[str, str] = {}
    for index, hit in enumerate(hits):
        job = hit.record.job
        payload = hit.record.model_dump_json()
        identifier = str(job.id)
        if identifier in identities and identities[identifier] != payload:
            raise DiscoveryConflict()
        identities[identifier] = payload
        keys: list[tuple[str, ...]] = [("id", identifier)]
        if job.source.external_id is not None:
            keys.append(("source", job.source.name, job.source.external_id))
        if job.source.canonical_url is not None:
            keys.append(("url", job.source.canonical_url))
        for key in keys:
            if key in seen:
                left, right = root(index), root(seen[key])
                parent[max(left, right)] = min(left, right)
            else:
                seen[key] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(hits)):
        groups.setdefault(root(index), []).append(index)
    result: list[JobGroup] = []
    for indexes in groups.values():
        result.append(
            JobGroup(
                selected=hits[indexes[0]],
                references=tuple(
                    SourceReference(
                        adapter_id=hits[index].source_id,
                        job_id=hits[index].record.job.id,
                        source=hits[index].record.job.source,
                        canonical_url=hits[index].record.job.source.canonical_url,
                    )
                    for index in indexes
                ),
            )
        )
    return DeduplicatedJobs(
        groups=tuple(result[: batch.limit]),
        duplicates_removed=len(hits) - len(result),
        more_available=batch.more_available or len(result) > batch.limit,
        searched_sources=batch.searched_sources,
    )
