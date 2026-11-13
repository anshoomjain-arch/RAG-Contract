import pytest

from insights.verify import Evidence, check, key_numbers

SOURCE = ("8.2.1 Subject to Clause 8.4, Airtel shall pay each invoice within thirty (30) days. The Non-Energy Base "
          "Rate is USD 1,456 per Site per calendar month. This Agreement is dated the 2nd day of November 2021. "
          "Target uptime is 99.85% and the discount is fifteen per cent.")


@pytest.mark.parametrize("value", [
    "Airtel pays invoices within 30 days",
    "Airtel pays invoices within thirty days",
    "USD 1,456.00 per Site per month",
    "USD 1456 per Site",
    "Dated 2 November 2021",
    "Dated November 2, 2021",
    "Dated 02/11/2021",
    "Signed in November 2021",
    "Uptime target 99.85%",
    "A 15% discount",
    "Payment under Clause 8.2.1 is due in 30 days",      # clause numbers are locations, not facts
])
def test_verified(value):
    assert check(value, Evidence(SOURCE))["status"] == "verified"


def test_ordinals_in_the_source():
    source = "For each Contract Year commencing with the third Contract Year through the twelfth Contract Year"
    assert check("Airtel may terminate from year 3 to 12", Evidence(source))["status"] == "verified"


@pytest.mark.parametrize("value, missing", [
    ("Airtel pays invoices within 45 days", ["45"]),
    ("USD 17,472 per Site per year", ["17,472"]),
    ("Dated 3 November 2021", ["3 November 2021"]),
    ("Signed in December 2021", ["December 2021"]),
    ("Uptime target 99.9%", ["99.9"]),
])
def test_unverified(value, missing):
    result = check(value, Evidence(SOURCE))
    assert result["status"] == "unverified"
    assert result["missing"] == missing


def test_key_numbers():
    quote = ("16.2.2 under this Agreement, thirty percent (30%) of revenues (deemed to be USD 4,001,400) within "
             "fifteen (15) Business Days, for 3 sites")
    assert key_numbers(quote) == {"30", "4001400", "15"}        # not the clause number or the plain count
    assert key_numbers("a cap of 99.85 per cent and 12 months") == {"99.85", "12"}


def test_no_values():
    assert check("Either party may assign with consent", Evidence(SOURCE))["status"] == "no_values"
    assert check("One of the parties may refer the dispute", Evidence(SOURCE))["status"] == "no_values"
