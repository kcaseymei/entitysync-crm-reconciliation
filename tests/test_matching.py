"""Matching tests. Each one mirrors a real situation found in the sandbox data."""
from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from matcher import match_all, match_location, normalize_name, normalize_street
from rules import WEAK_SURVIVOR_FLAG, build_plan


def plan_for(locations, accounts):
    matches, orphans = match_all(locations, accounts, PARENT_ID)
    return build_plan(matches, orphans, PARENT_ID, PARENT_NAME)


def kinds(plan):
    return sorted(p.kind for p in plan.proposals)


# ---- normalization ---------------------------------------------------------

def test_street_abbreviations_compare_equal():
    assert normalize_street("4850 Northwest Sylvania Avenue") == normalize_street("4850 NW Sylvania Ave")
    assert normalize_street("199 Barks Road West") == normalize_street("199 Barks Rd W")
    assert normalize_street("3313 Wilmington Pk") == normalize_street("3313 Wilmington Pike")


def test_street_numbers_are_not_normalized_away():
    assert normalize_street("750 Stewart Rd") != normalize_street("751 Stewart Rd")


def test_name_normalization_handles_common_variants():
    assert normalize_name("Bellhaven Rehab and Nursing") == normalize_name("Bellhaven Rehabilitation & Nursing")
    assert normalize_name("Health Care Center") == normalize_name("Healthcare Centre")


# ---- candidate retrieval ---------------------------------------------------

def test_same_address_different_format_is_high_confidence_with_no_changes():
    plan = plan_for([location("Bellhaven of Maplewood", "210 Orchard Lane")],
                    [account("A1", "Bellhaven of Maplewood", "210 Orchard Ln")])
    assert plan.proposals == []
    assert plan.unchanged[0].confidence == "high"


def test_zip_typo_still_matches_on_street_and_city():  # Portsmouth
    loc = location("Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", zip="45662")
    plan = plan_for([loc], [account("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", zip="45626")])
    [p] = plan.proposals
    assert p.kind == "UPDATE" and p.after == {"billing_zip": "45662"} and p.confidence == "medium"


def test_placeholder_address_matches_on_exact_name_in_same_city():  # Ashtabula
    loc = location("Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", zip="44004")
    plan = plan_for([loc], [account("A1", "Bellhaven of Ashtabula", "PO Box 517", "Ashtabula", zip="44004")])
    [p] = plan.proposals
    assert p.after == {"billing_street": "3156 W Prospect Rd"}


def test_same_name_in_another_city_is_not_a_match():  # Amberly Manor trap
    loc = location("Amberly Manor", "4390 Darrow Rd", "Hudson", zip="44236")
    other = account("A1", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "CO", "80903", parent_id="P_JUNIPER")
    plan = plan_for([loc], [other])
    assert kinds(plan) == ["CREATE"]


def test_renamed_facility_matches_by_address():  # Riverbend Manor -> Bellhaven of Chagrin Falls
    loc = location("Bellhaven of Chagrin Falls", "150 River St", "Chagrin Falls", zip="44022")
    plan = plan_for([loc], [account("A1", "Riverbend Manor Care Center", "150 River St", "Chagrin Falls", zip="44022")])
    [p] = plan.proposals
    assert p.kind == "UPDATE" and p.after == {"name": "Bellhaven of Chagrin Falls"}


def test_parent_accounts_are_never_candidates():
    plan = plan_for([location("X", "1 Main St")], [account("P1", "Some Co (Parent Account)", "1 Main St", parent_id="")])
    assert kinds(plan) == ["CREATE"]


# ---- duplicates ------------------------------------------------------------

def test_duplicates_keep_copy_under_correct_parent():  # Monroe / Erie / Port Clinton
    loc = location("Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162")
    plan = plan_for([loc], [
        account("A1", "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162"),
        account("A2", "Cedar Trail of Monroe", "750 Stewart Road", "Monroe", "MI", "48162", parent_id=OTHER_PARENT_ID),
    ])
    [p] = plan.proposals
    assert p.kind == "DUPLICATE" and p.subject == "account:A2"
    assert p.operations[0].fields["duplicate_of_account"] == "A1"
    assert p.operations[0].fields["status"] == "Inactive"


def test_survivor_prefers_billing_history_over_parent():
    loc = location("Bellhaven of X", "1 Main St")
    plan = plan_for([loc], [
        account("A1", "Bellhaven of X", "1 Main St"),
        account("A2", "Old Name of X", "1 Main St", parent_id=OTHER_PARENT_ID, revenue=5000),
    ])
    dup = next(p for p in plan.proposals if p.kind == "DUPLICATE")
    assert dup.subject == "account:A1"
    assert dup.operations[0].fields["duplicate_of_account"] == "A2"


def test_survivor_picked_only_by_soft_signals_is_low_confidence():  # Kettering
    loc = location("Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", zip="45429")
    plan = plan_for([loc], [
        account("A1", "Kettering Care Centre", "3313 Wilmington Pike", "Kettering", zip="45429", parent_id="P_HARBOR"),
        account("A2", "Kettering Senior Campus", "3313 Wilmington Pk", "Kettering", zip="45429", parent_id=OTHER_PARENT_ID),
    ])
    assert plan.proposals and all(p.confidence == "low" for p in plan.proposals)
    assert all(WEAK_SURVIVOR_FLAG in p.flags for p in plan.proposals)


def test_survivor_picked_by_hard_evidence_is_not_flagged_weak():  # Monroe
    loc = location("Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162")
    plan = plan_for([loc], [
        account("A1", "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162"),
        account("A2", "Cedar Trail of Monroe", "750 Stewart Road", "Monroe", "MI", "48162", parent_id=OTHER_PARENT_ID),
    ])
    [p] = plan.proposals
    assert p.confidence == "high" and WEAK_SURVIVOR_FLAG not in p.flags


def test_two_copies_with_billing_history_go_to_human_review():
    loc = location("Bellhaven of X", "1 Main St")
    plan = plan_for([loc], [account("A1", "Bellhaven of X", "1 Main St", revenue=100),
                            account("A2", "Bellhaven of X", "1 Main Street", revenue=200)])
    [p] = plan.proposals
    assert p.kind == "NEEDS_REVIEW" and p.confidence == "low"
    assert all(op.fields["status"] == "Needs Review" for op in p.operations)


def test_ambiguous_copies_already_flagged_are_not_proposed_again():
    loc = location("Bellhaven of X", "1 Main St")
    plan = plan_for([loc], [account("A1", "Bellhaven of X", "1 Main St", revenue=100, status="Needs Review"),
                            account("A2", "Bellhaven of X", "1 Main Street", revenue=200, status="Needs Review")])
    assert plan.proposals == []


def test_already_marked_duplicate_is_not_proposed_again():
    loc = location("Bellhaven of Owosso", "1120 W Main St")
    plan = plan_for([loc], [account("A1", "Bellhaven of Owosso", "1120 W Main St"),
                            account("A2", "Bellhaven of Owosso", "1120 West Main Street",
                                    status="Inactive", duplicate_of_account="A1")])
    assert plan.proposals == []


# ---- missing & orphans -----------------------------------------------------

def test_missing_location_creates_account_under_parent():
    loc = location("Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", zip="45103",
                   care=("Short-Term Rehabilitation & Nursing",))
    [p] = plan_for([loc], []).proposals
    fields = p.operations[0].fields
    assert p.kind == "CREATE" and p.subject == "location:bellhaven-of-batavia"
    assert fields["parent_id"] == PARENT_ID and fields["care_type"] == "Skilled Nursing"
    assert (fields["billing_street"], fields["billing_zip"]) == ("2000 Hospital Dr", "45103")


def test_orphan_is_flagged_for_review_not_moved():  # Sandusky
    orphan = account("A9", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", zip="44870", revenue=130000, ar=5200)
    [p] = plan_for([], [orphan]).proposals
    assert p.kind == "NEEDS_REVIEW"
    assert set(p.operations[0].fields) == {"status", "note"}  # no parent change, no inactivation


def test_chow_historical_account_is_not_flagged_as_orphan():
    old = account("A9", "Old Facility", "9 Elm St", revenue=1000, ar=10, chow_current_account="A10")
    assert plan_for([], [old]).proposals == []


def test_whitespace_in_city_does_not_cause_a_repeat_update():
    loc = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin ", zip="44883")
    plan = plan_for([loc], [account("A1", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin ", zip="44883")])
    assert plan.proposals == []


def test_orphans_under_other_parents_are_ignored():
    assert plan_for([], [account("A1", "Some Facility", "1 Main St", parent_id=OTHER_PARENT_ID)]).proposals == []


def test_one_location_one_candidate_lookup_is_exact_on_street_number():
    m = match_location(location("Bellhaven of X", "750 Stewart Rd"), [account("A1", "Other", "751 Stewart Rd")])
    assert m.candidates == []
