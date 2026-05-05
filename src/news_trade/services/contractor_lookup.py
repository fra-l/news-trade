"""Static contractor name → ticker lookup service.

Layer 1 of EntityResolutionService. Loads ``data/contractor_tickers.csv``
at construction time, builds an O(1) exact-match index from canonical names
and all aliases (both normalised), then falls back to difflib fuzzy matching
for minor name variations (e.g. punctuation differences, missing legal suffix).

The lookup never raises — unknown names return ``None``.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_logger = logging.getLogger(__name__)

_DEFAULT_CSV = Path(__file__).parents[1] / "data" / "contractor_tickers.csv"

_FUZZY_THRESHOLD = 0.82


@dataclass(frozen=True)
class LookupResult:
    """Result of a contractor name lookup."""

    ticker: str
    exchange: str
    lda_client_name: str
    confidence: float


def _normalize(name: str) -> str:
    """Normalise a company name for consistent comparison.

    Steps:
    1. Uppercase
    2. Replace ``&`` with ``AND``
    3. Replace all non-alphanumeric characters with a space
    4. Collapse runs of whitespace to a single space and strip
    """
    name = name.upper()
    name = name.replace("&", "AND")
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


class ContractorLookup:
    """Resolve USASpending recipient names to stock tickers.

    Uses an exact-match dict (O(1)) keyed by normalised canonical name and
    every pipe-separated alias. Falls back to difflib similarity scoring when
    no exact key is found.

    Args:
        csv_path: Path to the CSV file. Defaults to the bundled
            ``data/contractor_tickers.csv``.
    """

    def __init__(self, csv_path: Path | None = None) -> None:
        self._exact: dict[str, LookupResult] = {}
        self._all_keys: list[tuple[str, LookupResult]] = []
        self._load(csv_path or _DEFAULT_CSV)
        _logger.info(
            "ContractorLookup loaded %d exact-match keys from %s",
            len(self._exact),
            csv_path or _DEFAULT_CSV,
        )

    def _load(self, path: Path) -> None:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                canonical = row.get("canonical_name", "").strip()
                ticker = row.get("ticker", "").strip()
                exchange = row.get("exchange", "").strip()
                lda_name = row.get("lda_client_name", "").strip()

                if not canonical or not ticker:
                    continue

                result = LookupResult(
                    ticker=ticker,
                    exchange=exchange,
                    lda_client_name=lda_name,
                    confidence=1.0,
                )

                names: list[str] = [canonical]
                aliases_raw = row.get("aliases", "").strip()
                if aliases_raw:
                    names.extend(a.strip() for a in aliases_raw.split("|") if a.strip())

                for name in names:
                    key = _normalize(name)
                    if key:
                        self._exact[key] = result
                        self._all_keys.append((key, result))

    def lookup(self, name: str) -> LookupResult | None:
        """Return a ``LookupResult`` for *name*, or ``None`` if unresolvable.

        Tries exact match first; falls back to fuzzy match when the best
        similarity score meets ``_FUZZY_THRESHOLD``.

        Args:
            name: Recipient name exactly as returned by USASpending.

        Returns:
            ``LookupResult`` with ``confidence=1.0`` for exact matches,
            or the fuzzy similarity score for approximate matches.
            ``None`` when no match meets the threshold.
        """
        if not name:
            return None

        key = _normalize(name)

        exact = self._exact.get(key)
        if exact is not None:
            return exact

        return self._fuzzy_lookup(key)

    def _fuzzy_lookup(self, normalised_query: str) -> LookupResult | None:
        best_score = 0.0
        best_result: LookupResult | None = None

        for candidate_key, result in self._all_keys:
            score = SequenceMatcher(None, normalised_query, candidate_key).ratio()
            if score > best_score:
                best_score = score
                best_result = result

        if best_result is not None and best_score >= _FUZZY_THRESHOLD:
            _logger.debug(
                "Fuzzy match %.2f: '%s' → %s",
                best_score,
                normalised_query,
                best_result.ticker,
            )
            return LookupResult(
                ticker=best_result.ticker,
                exchange=best_result.exchange,
                lda_client_name=best_result.lda_client_name,
                confidence=round(best_score, 4),
            )

        return None

    @property
    def size(self) -> int:
        """Number of unique normalised keys in the exact-match index."""
        return len(self._exact)


if __name__ == "__main__":
    lookup = ContractorLookup()
    print(f"Loaded {lookup.size} keys.\n")

    known = [
        "LOCKHEED MARTIN CORPORATION",
        "PALANTIR TECHNOLOGIES INC - FEDERAL",
        "BOOZ ALLEN HAMILTON HOLDING CORPORATION",
        "SCIENCE APPLICATIONS INTERNATIONAL CORP",
        "GENERAL DYNAMICS INFORMATION TECHNOLOGY INC",
    ]
    unknown = [
        "ACME WIDGET COMPANY LLC",
        "RANDOM SERVICES INC",
        "UNKNOWN DEFENSE CONTRACTOR",
        "NO SUCH FIRM",
        "PRIVATE HOLDING GROUP",
    ]

    print("── Known contractors ──────────────────────────────────")
    for name in known:
        result = lookup.lookup(name)
        print(f"  {name!r:55s} → {result}")

    print("\n── Unknown contractors ────────────────────────────────")
    for name in unknown:
        result = lookup.lookup(name)
        print(f"  {name!r:55s} → {result}")
