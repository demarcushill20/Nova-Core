"""Enhanced skill ranking with BM25 pre-filtering and quality-based exclusion.

Upgrades from keyword substring matching to:
1. BM25 text ranking for better relevance
2. Quality-based auto-exclusion of poorly performing skills
3. Mid-execution skill retrieval support
"""
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RankedSkill:
    """A skill with its ranking score and quality metrics."""

    name: str
    description: str
    path: str
    bm25_score: float = 0.0
    quality_score: float = 1.0  # from version store health
    completion_rate: float = 1.0
    fallback_rate: float = 0.0
    combined_score: float = 0.0
    excluded: bool = False
    exclusion_reason: str = ""


class BM25Ranker:
    """BM25 text ranking for skill selection.

    Pre-filters skills by text relevance before the existing
    skill_scorer.py scoring pipeline.
    """

    # BM25 parameters
    K1 = 1.5  # term frequency saturation
    B = 0.75  # document length normalization

    # Quality-based exclusion thresholds
    MIN_SELECTIONS_FOR_EXCLUSION = 5
    COMPLETION_RATE_FLOOR = 0.2
    FALLBACK_RATE_CEIL = 0.5

    def __init__(self) -> None:
        self._corpus: list[dict] = []  # [{name, text, path}]
        self._idf: dict[str, float] = {}
        self._doc_lengths: list[int] = []
        self._avg_doc_length: float = 0.0
        self._tokenized_docs: list[list[str]] = []
        self._built = False

    def build_index(self, skills: list[dict]) -> None:
        """Build BM25 index from skill corpus.

        Args:
            skills: List of dicts with 'name', 'description', 'path',
                    and optional 'body'.
        """
        self._corpus = skills
        self._tokenized_docs = []

        for skill in skills:
            # Build document text: name + description + body[:2000]
            text = (
                f"{skill['name']} "
                f"{skill.get('description', '')} "
                f"{skill.get('body', '')[:2000]}"
            )
            tokens = self._tokenize(text)
            self._tokenized_docs.append(tokens)

        # Compute document lengths
        self._doc_lengths = [len(doc) for doc in self._tokenized_docs]
        self._avg_doc_length = (
            sum(self._doc_lengths) / max(len(self._doc_lengths), 1)
        )

        # Compute IDF
        n_docs = len(self._tokenized_docs)
        df: Counter[str] = Counter()
        for doc in self._tokenized_docs:
            unique_terms = set(doc)
            for term in unique_terms:
                df[term] += 1

        self._idf = {}
        for term, freq in df.items():
            # Standard BM25 IDF formula
            self._idf[term] = math.log(
                (n_docs - freq + 0.5) / (freq + 0.5) + 1.0
            )

        self._built = True
        logger.debug(
            "BM25 index built: %d skills, %d unique terms",
            n_docs,
            len(self._idf),
        )

    def rank(
        self,
        query: str,
        top_k: int = 10,
        quality_data: dict[str, dict] | None = None,
    ) -> list[RankedSkill]:
        """Rank skills by BM25 relevance + quality filtering.

        Args:
            query: The task description to rank against.
            top_k: Max number of results to return.
            quality_data: Optional dict of
                {skill_name: {completion_rate, fallback_rate, selections, ...}}.

        Returns:
            List of RankedSkill sorted by combined_score descending.
        """
        if not self._built:
            logger.warning("BM25 index not built, returning empty results")
            return []

        query_tokens = self._tokenize(query)

        # Score each document
        scored: list[RankedSkill] = []
        for idx, doc_tokens in enumerate(self._tokenized_docs):
            skill = self._corpus[idx]
            bm25_score = self._score_document(query_tokens, doc_tokens, idx)

            ranked = RankedSkill(
                name=skill["name"],
                description=skill.get("description", ""),
                path=skill.get("path", ""),
                bm25_score=bm25_score,
            )

            # Apply quality data if available
            if quality_data and skill["name"] in quality_data:
                qd = quality_data[skill["name"]]
                ranked.completion_rate = qd.get("completion_rate", 1.0)
                ranked.fallback_rate = qd.get("fallback_rate", 0.0)
                ranked.quality_score = qd.get("quality_score", 1.0)

                selections = qd.get("selections", 0)

                # Auto-exclusion
                if selections >= self.MIN_SELECTIONS_FOR_EXCLUSION:
                    if ranked.completion_rate < self.COMPLETION_RATE_FLOOR:
                        ranked.excluded = True
                        ranked.exclusion_reason = (
                            f"completion_rate={ranked.completion_rate:.2f}"
                            f" < {self.COMPLETION_RATE_FLOOR}"
                        )
                    elif ranked.fallback_rate > self.FALLBACK_RATE_CEIL:
                        ranked.excluded = True
                        ranked.exclusion_reason = (
                            f"fallback_rate={ranked.fallback_rate:.2f}"
                            f" > {self.FALLBACK_RATE_CEIL}"
                        )

            # Combined score: BM25 * quality
            ranked.combined_score = bm25_score * ranked.quality_score
            scored.append(ranked)

        # Sort by combined score, non-excluded first
        scored.sort(
            key=lambda s: (not s.excluded, s.combined_score), reverse=True
        )

        # Return top_k non-excluded + any excluded (for logging)
        non_excluded = [s for s in scored if not s.excluded]
        excluded = [s for s in scored if s.excluded]

        result = non_excluded[:top_k]

        # Log exclusions
        for s in excluded:
            logger.info(
                "Skill auto-excluded: %s (%s)", s.name, s.exclusion_reason
            )

        return result

    def _score_document(
        self,
        query_tokens: list[str],
        doc_tokens: list[str],
        doc_idx: int,
    ) -> float:
        """Compute BM25 score for a single document."""
        doc_len = self._doc_lengths[doc_idx]
        score = 0.0

        tf: Counter[str] = Counter(doc_tokens)

        for term in query_tokens:
            if term not in self._idf:
                continue

            term_freq = tf.get(term, 0)
            idf = self._idf[term]

            # BM25 formula
            numerator = term_freq * (self.K1 + 1)
            denominator = term_freq + self.K1 * (
                1 - self.B + self.B * doc_len / max(self._avg_doc_length, 1)
            )
            score += idf * (numerator / denominator)

        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize text into lowercase terms."""
        # Split on non-alphanumeric, lowercase, filter short terms
        tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
        return [t for t in tokens if len(t) > 1]  # skip single chars

    def get_corpus_size(self) -> int:
        """Return the number of indexed skills."""
        return len(self._corpus)
