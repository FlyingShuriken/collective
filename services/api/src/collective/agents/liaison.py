from collective.adapters.models import Task
from collective.agents.schemas import DraftReading, QuoteReading
from collective.api.dto import Evidence

QUOTE = Task(
    "quote",
    "1",
    "liaison-quote-v1",
    "Read only the supplied supplier reply into a quote proposal. Values are decimal strings. "
    "RM denotes MYR. Preserve the supplier's units and explicit tier, delivery, tax, credit, "
    "validity and pooled independent-buyer conditions. Unknown fees are null, never zero. "
    "Leave unstated pack multiples, discounts, surcharges and delivery dates null. "
    "A date without a precise expiry time stays in validity_text; do not invent an instant. "
    "List missing, ambiguous, unreadable or unsupported fields as issues. "
    "Do not infer contractual terms from other quotes. Text evidence uses character offsets; "
    "image evidence uses approximate normalized page coordinates or a full-page fallback. "
    "Report a confidence label for review only. Never supply database IDs or instructions.",
)
DRAFT = Task(
    "draft",
    "1",
    "liaison-draft-v1",
    "Draft one concise supplier RFQ or invoice query from the supplied authorized facts only. "
    "Use the supplied language. For an RFQ ask for price tiers, delivery fees, tax treatment, "
    "credit, validity and whether independent buyers may pool with separate invoices/deliveries. "
    "State the selected quantity, specification and requested delivery window. "
    "For an invoice query describe the recorded differences from the issued PO without "
    "claiming supplier wrongdoing or a confirmed recovery. Do not invent facts, commitments, "
    "other buyers or negotiated terms. Return draft text only; never choose a recipient or send.",
)


async def parse(gateway, document):
    return await gateway.extract(QUOTE, document, QuoteReading, "fast")


async def draft(gateway, document):
    return await gateway.extract(DRAFT, document, DraftReading, "fast")


def review_flags(reading):
    required = (
        "currency",
        "base_unit",
        "pack_multiple",
        "tier_kind",
        "per_business_moq",
        "aggregate_moq",
        "delivery_mode",
        "delivery_fee",
        "credit_days",
        "tax_rate",
        "discount",
        "surcharge",
        "conditions",
        "valid_from",
        "valid_until",
        "arrival_start",
        "arrival_end",
    )
    flags = [f"{key}:missing" for key in required if getattr(reading, key) is None]
    flags.extend(f"{issue.field_path}:{issue.reason}" for issue in reading.issues)
    if not reading.tiers:
        flags.append("tiers:missing")
    if not reading.conditions_complete:
        flags.append("conditions:incomplete")
    if reading.currency not in {None, "MYR"}:
        flags.append("currency:unsupported")
    if reading.confidence_label != "high":
        flags.append("reading:review")
    return list(dict.fromkeys(flags))


def evidence(reading, document):
    result, invalid = [], False
    for anchor in reading.evidence:
        value = anchor.model_dump() | {
            "source_id": document["source_id"],
            "confidence_label": getattr(anchor, "confidence_label", None)
            or reading.confidence_label,
        }
        try:
            if anchor.region_quality == "text":
                start, end = anchor.text_span or (-1, -1)
                text = document["text"]
                if (
                    text is None
                    or not 0 <= start < end <= len(text)
                    or anchor.extracted_value is None
                    or text[start:end] != anchor.extracted_value
                ):
                    raise ValueError("Evidence does not match the saved source")
            elif anchor.page not in {page["page"] for page in document["pages"]}:
                raise ValueError("Evidence page does not exist")
            result.append(Evidence.model_validate(value).model_dump(mode="json"))
        except ValueError:
            invalid = True
    if not result:
        text = document["text"]
        fallback = {
            "field_path": "quote",
            "source_id": document["source_id"],
            "confidence_label": "review",
            "extracted_value": None,
        }
        if text:
            fallback |= {"region_quality": "text", "text_span": (0, len(text))}
        else:
            fallback |= {"region_quality": "full_page", "page": 1}
        result.append(Evidence.model_validate(fallback).model_dump(mode="json"))
    return result, invalid
