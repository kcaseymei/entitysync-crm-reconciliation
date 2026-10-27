"""Small builders for test accounts and locations, shaped like the real API/scraper output."""
from scraper import Location

PARENT_ID = "P_BELLHAVEN"
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
OTHER_PARENT_ID = "P_CEDAR"
OTHER_PARENT_NAME = "Cedar Trail Communities (Parent Account)"


def account(account_id, name, street, city="Akron", state="OH", zip="44313", *, parent_id=PARENT_ID,
            revenue=0, ar=0, status="Active", care_type="Assisted Living", **extra):
    parent_name = {PARENT_ID: PARENT_NAME, OTHER_PARENT_ID: OTHER_PARENT_NAME}.get(parent_id, "")
    return {
        "account_id": account_id, "name": name, "parent_id": parent_id, "parent_name": parent_name,
        "billing_street": street, "billing_city": city, "billing_state": state, "billing_zip": zip,
        "care_type": care_type, "status": status, "phone": "", "lifetime_revenue": revenue, "outstanding_ar": ar,
        "chow_current_account": "", "duplicate_of_account": "", "note": "", "created_by_candidate": False,
        **extra,
    }


def location(name, street, city="Akron", state="OH", zip="44313", care=("Assisted Living",), slug=None):
    slug = slug or name.lower().replace(" ", "-")
    return Location(slug=slug, name=name, street=street, city=city, state=state, zip=zip, care_offerings=tuple(care),
                    phone="(555) 000-0000", administrator="Pat Lee", source_url=f"https://site.test/communities/{slug}")
