from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from django.db.models import Q

from .importers import normalize_text
from .models import Counterparty, CounterpartyAlias, Origin


SOURCE_PRIORITY = {
    Origin.MANUAL: 6,
    Origin.IMPORT: 5,
    Origin.OFX: 4,
    Origin.TELEGRAM: 3,
    Origin.AI: 2,
    Origin.HISTORICAL: 1,
}


def counterparty_rank(counterparty: Counterparty) -> tuple[int, int, Decimal, int]:
    return (
        document_score(counterparty),
        SOURCE_PRIORITY.get(counterparty.source, 0),
        counterparty.confidence or Decimal("0"),
        -counterparty.pk,
    )


def document_score(counterparty: Counterparty) -> int:
    if counterparty.primary_document:
        return 2
    if has_related_document(counterparty):
        return 1
    return 0


def has_related_document(counterparty: Counterparty) -> bool:
    prefetched = getattr(counterparty, "_prefetched_objects_cache", {})
    if "documents" in prefetched:
        return any(document.number for document in prefetched["documents"])
    return counterparty.documents.exists()


def choose_best_counterparty(counterparties: Iterable[Counterparty]) -> Counterparty | None:
    unique = {counterparty.pk: counterparty for counterparty in counterparties}
    if not unique:
        return None
    return max(unique.values(), key=counterparty_rank)


def find_best_counterparty_by_normalized_name(
    normalized_name: str,
    *,
    kind: str = "",
) -> Counterparty | None:
    if not normalized_name:
        return None
    queryset = Counterparty.objects.filter(normalized_name=normalized_name, is_active=True).prefetch_related(
        "documents"
    )
    matches = list(queryset)
    if kind:
        same_kind = [counterparty for counterparty in matches if counterparty.kind == kind]
        if same_kind:
            return choose_best_counterparty(same_kind)
    return choose_best_counterparty(matches)


def find_best_counterparty_by_name(name: str, *, kind: str = "") -> Counterparty | None:
    normalized_name = normalize_text(name)
    if not normalized_name:
        return None
    queryset = Counterparty.objects.filter(
        Q(normalized_name=normalized_name) | Q(name__iexact=name),
        is_active=True,
    ).prefetch_related("documents")
    matches = list(queryset)
    if kind:
        same_kind = [counterparty for counterparty in matches if counterparty.kind == kind]
        if same_kind:
            return choose_best_counterparty(same_kind)
    return choose_best_counterparty(matches)


def find_best_counterparty_by_name_or_alias(name: str, *, kind: str = "") -> Counterparty | None:
    normalized_name = normalize_text(name)
    counterparty = find_best_counterparty_by_normalized_name(normalized_name, kind=kind)
    if counterparty:
        return counterparty

    alias_matches = CounterpartyAlias.objects.select_related("counterparty").filter(
        normalized_name=normalized_name,
        counterparty__is_active=True,
    )
    counterparties = [alias.counterparty for alias in alias_matches]
    if kind:
        same_kind = [counterparty for counterparty in counterparties if counterparty.kind == kind]
        if same_kind:
            return choose_best_counterparty(same_kind)
    return choose_best_counterparty(counterparties)


def pick_counterparty_candidate(
    candidates: Iterable[tuple[int, Counterparty]],
) -> Counterparty | None:
    scored = [
        (match_length, counterparty_rank(counterparty), counterparty)
        for match_length, counterparty in candidates
    ]
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def context_counterparties(limit: int) -> list[Counterparty]:
    queryset = (
        Counterparty.objects.filter(is_active=True)
        .select_related("default_category", "default_cost_center", "default_work")
        .prefetch_related("documents")
    )
    selected: list[Counterparty] = []
    seen_names: set[str] = set()
    for counterparty in sorted(queryset, key=counterparty_rank, reverse=True):
        normalized_name = counterparty.normalized_name or normalize_text(counterparty.name)
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        selected.append(counterparty)
        if len(selected) >= limit:
            break
    return selected
