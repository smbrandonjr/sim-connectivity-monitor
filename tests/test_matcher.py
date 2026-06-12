from sim_monitor.config.matcher import match_profile, normalize_iccid
from sim_monitor.config.schema import Profile


def make_profile(name, patterns, priority=100):
    return Profile.model_validate(
        {
            "name": name,
            "match": {"iccid_patterns": patterns, "priority": priority},
            "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
        }
    )


ICCID = "8944500612345678901"


class TestNormalizeIccid:
    def test_plain(self):
        assert normalize_iccid(ICCID) == ICCID

    def test_strips_whitespace_and_separators(self):
        assert normalize_iccid(" 8944 5006-1234567890 1 ") == ICCID

    def test_strips_trailing_f_filler(self):
        assert normalize_iccid("8944500612345678901F") == ICCID
        assert normalize_iccid("8944500612345678901f") == ICCID


class TestMatchProfile:
    def test_no_profiles(self):
        assert match_profile(ICCID, []) is None

    def test_no_match(self):
        p = make_profile("other", ["8999*"])
        assert match_profile(ICCID, [p]) is None

    def test_exact_match(self):
        p = make_profile("exact", [ICCID])
        result = match_profile(ICCID, [p])
        assert result.profile.name == "exact"
        assert result.exact

    def test_exact_beats_prefix(self):
        prefix = make_profile("prefix", ["8944500*"], priority=1)
        exact = make_profile("exact", [ICCID], priority=999)
        result = match_profile(ICCID, [prefix, exact])
        assert result.profile.name == "exact"

    def test_longer_prefix_beats_shorter(self):
        short = make_profile("short", ["8944*"], priority=1)
        long = make_profile("long", ["89445006*"], priority=999)
        result = match_profile(ICCID, [short, long])
        assert result.profile.name == "long"

    def test_priority_breaks_prefix_ties(self):
        a = make_profile("a", ["8944500*"], priority=50)
        b = make_profile("b", ["8944500*"], priority=10)
        result = match_profile(ICCID, [a, b])
        assert result.profile.name == "b"

    def test_name_breaks_full_ties(self):
        z = make_profile("zeta", ["8944*"])
        a = make_profile("alpha", ["8944*"])
        result = match_profile(ICCID, [z, a])
        assert result.profile.name == "alpha"

    def test_catchall_star_matches_everything(self):
        default = make_profile("default", ["*"], priority=1000)
        result = match_profile(ICCID, [default])
        assert result.profile.name == "default"
        assert not result.exact

    def test_specific_beats_catchall(self):
        default = make_profile("default", ["*"], priority=1000)
        specific = make_profile("specific", ["8944500*"], priority=10)
        result = match_profile(ICCID, [default, specific])
        assert result.profile.name == "specific"

    def test_iccid_with_filler_matches_clean_pattern(self):
        p = make_profile("exact", [ICCID])
        result = match_profile(ICCID + "F", [p])
        assert result.profile.name == "exact"
        assert result.exact

    def test_best_pattern_within_single_profile(self):
        p = make_profile("multi", ["*", ICCID])
        result = match_profile(ICCID, [p])
        assert result.exact
        assert result.pattern == ICCID
