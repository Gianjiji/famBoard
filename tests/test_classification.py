"""Functional + regression: domain -> category classification."""
from famboard.ingest import classify_domain


def test_curated_exact(conn):
    assert classify_domain(conn, "youtube.com", {}) == "entertainment"


def test_subdomain_matches_parent(conn):
    assert classify_domain(conn, "www.v2.facebook.com", {}) == "social"


def test_sensitive_domain(conn):
    assert classify_domain(conn, "samaritans.org", {}) == "self_harm"
    assert classify_domain(conn, "www.myproana.com", {}) == "eating_disorder"


def test_unknown_domain_is_uncategorized(conn):
    assert classify_domain(conn, "some-random-nonexistent-xyz.example", {}) is None


def test_cache_is_used(conn):
    cache = {}
    classify_domain(conn, "netflix.com", cache)
    assert "netflix.com" in cache and cache["netflix.com"] == "entertainment"
