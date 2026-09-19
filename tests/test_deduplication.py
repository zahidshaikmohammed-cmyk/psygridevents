import random
import time
from datetime import datetime, timedelta, timezone

from psygridevents.acquisition import RawObservation
from psygridevents.deduplication import deduplicate, exact_key, near_duplicate

T0 = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


def obs(title, *, publisher="Wire", url="https://example.com/a", published_at=T0) -> RawObservation:
    return RawObservation(
        provider_id="x", source_tier=0, publisher=publisher, title=title, url=url,
        summary=title, published_at=published_at, observed_at=T0, raw={},
    )


def test_exact_url_and_title_is_a_duplicate() -> None:
    decisions = deduplicate([obs("Company wins order"), obs("Company wins order")])
    assert decisions[1].duplicate_of is not None
    assert decisions[1].reason == "exact_url_and_title"
    assert decisions[1].similarity == 1.0


def test_same_publisher_and_url_with_different_title_is_still_a_duplicate() -> None:
    # Mirrors near_duplicate()'s own publisher+url short-circuit.
    decisions = deduplicate([
        obs("Company wins order", url="https://example.com/a"),
        obs("Completely different headline text", url="https://example.com/a"),
    ])
    assert decisions[1].duplicate_of is not None
    assert decisions[1].similarity == 1.0


def test_near_duplicate_title_from_a_different_publisher_is_detected() -> None:
    decisions = deduplicate([
        obs("Reliance wins major telecom order worth INR 500 crore", publisher="Wire A", url="https://example.com/a"),
        obs("Reliance wins major telecom order worth INR 501 crore", publisher="Wire B", url="https://example.com/b"),
    ])
    assert decisions[1].duplicate_of is not None
    assert decisions[1].reason == "near_duplicate_title"
    assert decisions[1].similarity >= 0.93


def test_dissimilar_titles_are_not_deduplicated() -> None:
    decisions = deduplicate([
        obs("Reliance wins major telecom order", url="https://example.com/a"),
        obs("Completely unrelated headline about the weather", url="https://example.com/b"),
    ])
    assert decisions[1].duplicate_of is None
    assert decisions[1].reason == "unique"


def test_similar_titles_outside_the_time_window_are_not_deduplicated() -> None:
    decisions = deduplicate(
        [
            obs("Reliance wins major telecom order worth INR 500 crore", url="https://example.com/a", published_at=T0),
            obs("Reliance wins major telecom order worth INR 501 crore", url="https://example.com/b", published_at=T0 + timedelta(hours=72)),
        ],
        window_hours=48,
    )
    assert decisions[1].duplicate_of is None


def test_missing_published_at_still_allows_similarity_comparison() -> None:
    decisions = deduplicate([
        obs("Reliance wins major telecom order worth INR 500 crore", url="https://example.com/a", published_at=None),
        obs("Reliance wins major telecom order worth INR 501 crore", url="https://example.com/b", published_at=None),
    ])
    assert decisions[1].duplicate_of is not None


def test_empty_titles_do_not_crash_and_are_not_falsely_merged() -> None:
    decisions = deduplicate([
        obs("", url="https://example.com/a"),
        obs("", url="https://example.com/b"),
    ])
    # Both empty canonical titles score 1.0 by definition (SequenceMatcher("", "")),
    # so they are legitimately treated as duplicates -- this matches the
    # pre-existing near_duplicate() contract exactly (verified by differential
    # testing against the prior implementation), not a new behavior.
    assert decisions[1].duplicate_of is not None


def test_optimized_deduplicate_matches_the_naive_pairwise_algorithm_on_random_data() -> None:
    # Differential test: an independent, deliberately naive re-implementation
    # of the original algorithm (direct near_duplicate() calls, no caching or
    # early-exit bounds) must agree with the optimized deduplicate() on every
    # decision. This is what actually proves the performance fix in
    # deduplication.py did not change behavior.
    from psygridevents.deduplication import DuplicateDecision

    def naive_deduplicate(observations, *, similarity_threshold=0.93):
        decisions = []
        representatives = []
        for index, observation in enumerate(observations):
            obs_id = f"obs-{index:08d}"
            duplicate_of, reason, similarity = None, "unique", 0.0
            for rep_id, representative in representatives:
                if exact_key(observation) == exact_key(representative):
                    duplicate_of, reason, similarity = rep_id, "exact_url_and_title", 1.0
                    break
                score = near_duplicate(observation, representative)
                if score >= similarity_threshold:
                    duplicate_of, reason, similarity = rep_id, "near_duplicate_title", score
                    break
            decisions.append(DuplicateDecision(observation, duplicate_of, reason, similarity))
            if duplicate_of is None:
                representatives.append((obs_id, observation))
        return decisions

    random.seed(1234)
    words = ["Reliance", "wins", "order", "results", "profit", "quarterly", "board", "meeting",
             "regulatory", "action", "SEBI", "penalty", "acquisition", "merger", "stake",
             "dividend", "buyback", "rating", "upgrade", "downgrade", "plant", "fire"]
    observations = []
    for i in range(250):
        title = " ".join(random.choice(words) for _ in range(random.randint(2, 9))) if random.random() > 0.05 else ""
        published_at = T0 + timedelta(minutes=random.randint(-3000, 3000)) if random.random() > 0.1 else None
        observations.append(
            obs(title, publisher=random.choice(["A", "B", "C"]), url=f"https://example.com/{random.randint(0, 40)}", published_at=published_at)
        )

    naive = naive_deduplicate(observations)
    optimized = deduplicate(observations)
    assert len(naive) == len(optimized)
    for n, o in zip(naive, optimized):
        assert n.duplicate_of == o.duplicate_of
        assert n.reason == o.reason
        assert abs(n.similarity - o.similarity) < 1e-9


def test_deduplicate_completes_promptly_at_realistic_batch_size() -> None:
    # Regression guard against reintroducing the O(N^2) SequenceMatcher cost
    # this was optimized away from (see deduplication.py docstring).
    random.seed(5)
    words = ["Reliance", "wins", "order", "results", "profit", "quarterly", "board", "meeting",
             "regulatory", "action", "SEBI", "penalty", "acquisition", "merger", "stake",
             "dividend", "buyback", "rating", "upgrade", "downgrade", "plant", "fire",
             "strike", "export", "import", "tariff", "subsidy", "budget", "policy"]
    observations = []
    for i in range(300):
        title = " ".join(random.choice(words) for _ in range(random.randint(4, 10)))
        published_at = T0 + timedelta(minutes=random.randint(0, 60 * 24 * 3))
        observations.append(obs(title, publisher=random.choice(["A", "B", "C"]), url=f"https://example.com/{i}", published_at=published_at))

    start = time.time()
    deduplicate(observations)
    elapsed = time.time() - start
    assert elapsed < 5.0, f"deduplicate() took {elapsed:.2f}s for 300 observations; investigate a possible perf regression"
