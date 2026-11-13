"""Key-information fields (SPEC section 7) and citation labels.

The fields are generic to long-form commercial contracts; nothing here is specific to one document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hint: str            # what belongs in the field; shown to the model


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    fields: tuple[Field, ...]


SECTIONS = (
    Section("overview", "Overview", (
        Field("parties", "Parties",
              "each party to this agreement: legal name, short name or role, and registered office"),
        Field("agreement_type", "Agreement type", "what kind of agreement this is and what it covers"),
        Field("agreement_date", "Agreement date",
              "the date this agreement itself is made, dated or signed (not dates of other agreements it mentions)"),
        Field("effective_date", "Effective date", "when the agreement or its obligations take effect"),
        Field("term", "Term", "how long the agreement, or the contracts made under it, last"),
        Field("renewal", "Renewal", "renewal or extension of the term"),
    )),
    Section("commercial", "Commercial", (
        Field("fees_and_rates", "Fees and rates", "base fees, rates and charges, with amounts and currency"),
        Field("premiums_and_discounts", "Premiums and discounts", "surcharges, premiums, discounts and rebates"),
        Field("escalation", "Escalation and indexation", "how charges change over time: indices, formulas, caps"),
        Field("payment_terms", "Payment terms", "when payment is due, late-payment interest, disputed amounts"),
        Field("invoicing", "Invoicing", "invoice timing, frequency, currency and contents"),
        Field("taxes", "Taxes", "tax treatment: VAT, withholding, gross-up, tax changes"),
    )),
    Section("commitments", "Commitments", (
        Field("volume_commitments", "Volume and site commitments",
              "minimum volumes or numbers of sites and the time windows for them"),
        Field("commitment_shortfall", "If a commitment is missed",
              "penalties, payments, credits or other consequences of missing a volume or site commitment "
              "(missed service levels belong under service levels)"),
    )),
    Section("service_levels", "Service levels", (
        Field("availability_targets", "Availability targets", "uptime or availability targets and how they are measured"),
        Field("service_remedies", "Remedies", "service credits, caps on credits and other remedies for missed levels"),
        Field("downtime_rules", "Downtime rules", "planned versus unplanned downtime, notice, exclusions"),
    )),
    Section("termination", "Term and termination", (
        Field("termination_rights", "Termination rights", "who may terminate, on what grounds and with what notice"),
        Field("termination_consequences", "Consequences of termination",
              "payments, exit amounts, removal of equipment, transition, survival"),
    )),
    Section("risk", "Risk allocation", (
        Field("liability_caps", "Liability caps", "limits on each party's liability"),
        Field("liability_exclusions", "Exclusions", "losses excluded from liability, and what is never limited"),
        Field("indemnities", "Indemnities", "who indemnifies whom and for what"),
        Field("insurance", "Insurance", "insurance each party must hold"),
        Field("force_majeure", "Force majeure", "what counts as force majeure and its consequences"),
    )),
    Section("other", "Other provisions", (
        Field("assignment", "Assignment", "assignment, transfer and change of control"),
        Field("confidentiality", "Confidentiality", "confidentiality duties and their exceptions"),
        Field("dispute_resolution", "Disputes and governing law",
              "escalation process, arbitration or courts, seat, rules, governing law, process agent"),
        Field("anti_bribery_sanctions", "Anti-bribery and sanctions", "anti-corruption, sanctions and compliance duties"),
        Field("change_control", "Change control", "how the agreement or services can be changed"),
    )),
)

FIELDS = {f.key: f for s in SECTIONS for f in s.fields}
SECTION_OF = {f.key: s.key for s in SECTIONS for f in s.fields}

SCHEDULE_ID = re.compile(r"S(\d+)(?::(.+))?$")


def clause_label(clause_id: str | None, chunk_kind: str, heading: str | None) -> str:
    """Human citation label: 'Clause 8.2', 'Schedule 4, para 3.1', 'Clause 1.1, "Effective Date"'."""
    cid = clause_id or ""
    if chunk_kind == "definition":
        return f'Clause {cid}, "{heading}"' if cid else f'"{heading}"'
    m = SCHEDULE_ID.match(cid)
    if m:
        sched, rest = m.groups()
        if not rest:
            return f"Schedule {sched}"
        if rest.startswith("form:"):
            return f"Schedule {sched}, {heading}"
        return f"Schedule {sched}, para {rest}"
    if cid[:1].isdigit():
        return f"Clause {cid}"
    return heading or cid or "Document"


def unit_key(clause_id: str | None, schedule_no: str | None) -> str:
    """Top-level clause or schedule a chunk belongs to: '8', 'S4', 'preamble', 'execution'."""
    if schedule_no:
        return f"S{schedule_no}"
    cid = clause_id or "other"
    return cid.split(".")[0] if cid[:1].isdigit() else cid
