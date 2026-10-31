"""Generate evidence-conscious narration and validate imported script files."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import random
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

from config.settings import FACT_CATEGORIES, GEMINI_MODEL
from pipeline.storyboard import STORYBOARD_SCHEMA, validate_storyboard


SYSTEM_PROMPT = """Write a compelling, accurate short educational narration.
Choose one well-established, specific fact that can be supported by primary sources
(a museum, university, scientific paper, government agency, or original archive).
Start with a concrete factual challenge, counterintuitive consequence, or question in at most 10 words.
Let the viewer make a prediction when the fact naturally supports it. Never force a quiz.
The opening visual must not give away a prediction's answer before the reveal.
Give the viewer a real question to resolve, then reveal its answer through the story.
Make the opening concrete and interesting without inventing danger, mystery, or drama.
Explain what happens and why, and end with a useful, memorable takeaway.
Write for the ear: sound like a curious person telling a friend something interesting.
Use contractions, direct language, and a mix of short and medium-length sentences.
Open with a concrete hook, then connect ideas smoothly. Explain unfamiliar terms simply.
Avoid textbook introductions, long lists, stacked clauses, and generic closing praise.
Cut filler such as 'To answer that question', 'The secret comes down to', 'The takeaway is',
and 'Next time you'. End on the answer itself, not a recap or a request to subscribe.
Use a hook, a clear explanation, and a specific final payoff that answers the opening.
Do not end with generic statements about humanity, resilience, or the wonders of science.
Do not use 'you won't believe', 'scientists are shocked', or unsupported superlatives.
Use ordinary punctuation to create natural breaths; do not add performance directions.
Distinguish demonstrated findings from hypotheses, estimates, and disputed accounts.
Avoid medical advice, sensational psychology claims, conspiracy claims, and viral myths.
Never invent quotations, statistics, research, or references. If unsure of a claim or
source, select a better-supported topic. Do not claim to have checked a URL or verified
facts: the output is a draft requiring human source review.

Return only the requested JSON object. The title must be at most 70 characters.
Check that the title agrees with the narrated answer; never reverse the result to make a hook.
The description should be two useful sentences, naming the actual subject naturally
in the first sentence. Do not put hashtags or keyword lists in the description.
Include 5-12 relevant tags ordered from specific to broad: first the exact subject
or named phenomenon, then closely related concepts, then one or two topic categories.
Prefer concise natural search phrases such as "golf ball dimples", "air resistance",
and "physics". Every tag must describe this narration; do not add unrelated trends,
viral, fyp, trending, subscribe, or generic filler. Do not prefix tags with #.
The narration across all storyboard beats must contain 80-105 spoken words, no headings, markdown, emojis,
stage directions, or URLs. This is a word target, not a guaranteed running time.
Aim for a 30-40 second experiment at conversational pace; never rush to meet a duration.
Return a storyboard with 8-12 short beats: hook, brief setup, explanation, final reveal.
Write the complete narration once, in order, across these beats without repeating or
omitting passages. Do not return a separate fact_text field: the application joins
the beats to create it. Aim for 8-14 spoken words and
roughly 2-4 seconds per beat. Each beat should advance the visual explanation.
Every beat has a visual with kind, headline (at most 80 characters), query, and labels.
Use photo for concrete objects/places, question for a prediction without the answer,
comparison for two factual alternatives (exactly 2 labels), or sequence for 2-3 causal steps.
Keep headlines to 2-6 words and labels to 2-7 words (maximum 64 characters).
Photo queries must be 2-4 words naming a visible subject or action, e.g. 'golf ball',
'writing notebook', 'octopus swimming'. They can retrieve footage or photos.
Never request an abstract metaphor such as 'fading memories' or 'invisible forces'.
Use empty labels for photos, and an empty query for diagrams/questions.
Use photo visuals for at least 80 percent of beats unless a supported subject animation
explains the fact better. Include the opening hook as a subject image, with the question
spoken in narration rather than written on a card. Vary concrete views of the subject.
Use at most one generic text/diagram beat; no paragraph slides.
Use a comparison or sequence only when the relationship is clearer than subject footage.
Diagram labels must only restate sourced claims in the narration.
Prefer a subject demonstration to abstract text boxes. For the specific topic of golf-ball
dimples, kind="animation", template="golf_airflow" supports views question, overview,
smooth, drag, dimples, attached, wake, comparison. These show qualitative airflow around
illustrated balls. Choose the view matching the spoken beat, with empty query and labels.
Do not use this template for another topic, or invent unsupported animation templates.
For list recall and the serial-position effect, kind="animation", template="memory_list"
supports question, overview, primacy, recency, middle, comparison. It shows an illustrated
example list with objects and shifting emphasis, not a measured chart or a diagnostic test.
Use these views instead of generic memory metaphors or decorative brain pictures.
Do not invent measurements, imply an experiment was performed, or use decorative brain imagery.
Provide 1-5 source objects, each with a descriptive title, a specific HTTPS URL,
and a nonempty claim explaining what that source is expected to support.
Use the exact JSON keys title, url, and claim for every source. Source URLs must not be guesses.
"""

FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 12},
        "storyboard": {**STORYBOARD_SCHEMA, "minItems": 8, "maxItems": 12},
        "sources": {
            "type": "array", "minItems": 1, "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {key: {"type": "string"} for key in ("title", "url", "claim")},
                "required": ["title", "url", "claim"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "description", "tags", "sources", "storyboard"],
    "additionalProperties": False,
}


def slugify(text: str) -> str:
    """Return a bounded filename component, including for non-Latin titles."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:50].rstrip("-")
    return slug or f"fact-{hashlib.sha256(str(text).encode()).hexdigest()[:10]}"


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    result = " ".join(value.split())
    if len(result) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return result


def _source_url(value: object) -> str:
    url = _text(value, "source.url", 2048)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or "." not in host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or any(char.isspace() for char in url)):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if host.endswith((".localhost", ".local", ".internal")):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except ValueError:
        raise ValueError("source.url must be a public HTTP(S) URL") from None
    return url


def _editorial_notes(fact: dict) -> list[str]:
    """Editorial targets guide review; structural and measured duration limits are separate."""
    notes = []
    if not 80 <= fact["word_count"] <= 105:
        notes.append(f"Narration has {fact['word_count']} words; the shorter-script experiment targets 80-105. Review the pacing.")
    if len(re.split(r"[.!?]", fact["fact_text"], maxsplit=1)[0].split()) > 10:
        notes.append("Opening exceeds 10 words; consider a quicker factual question or consequence.")
    if re.search(r"\b(next time you|to answer that question|the takeaway is)\b", fact["fact_text"], re.I):
        notes.append("Cut setup or recap filler and let the factual answer end the story.")
    if len(fact["title"]) > 70:
        notes.append(f"Title has {len(fact['title'])} characters; consider shortening it to 70 or fewer.")
    if not 5 <= len(fact["tags"]) <= 12:
        notes.append("Consider using 5-12 focused tags.")
    if len(fact["sources"]) > 5:
        notes.append("Consider keeping the 1-5 references that best support this script.")
    storyboard = fact.get("storyboard", [])
    if any(len(b["narration"].split()) > 18 for b in storyboard):
        notes.append("Some storyboard beats exceed 18 words. Break up long visual holds.")
    return notes


def validate_fact(data: object, category: str | None = None) -> dict:
    """Validate and normalize a generated or user-provided script without mutating it.

    This validates structure, not truth. Missing sources remain visible in metadata;
    neither an input flag nor a model assertion can mark a script as verified.
    """
    if not isinstance(data, dict):
        raise ValueError("Script must be a JSON object")
    title = _text(data.get("title"), "title", 100)
    description = _text(data.get("description"), "description", 4500)
    narration = _text(data.get("fact_text"), "fact_text", 6000)
    word_count = len(narration.split())
    if not 10 <= word_count <= 400:
        raise ValueError("fact_text must contain 10-400 words")
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not 1 <= len(raw_tags) <= 20:
        raise ValueError("tags must be a list containing 1-20 strings")
    tags = list(dict.fromkeys(_text(tag, "tag", 80).lstrip("#") for tag in raw_tags))
    if any(not tag for tag in tags) or sum(len(tag) + 1 for tag in tags) > 450:
        raise ValueError("tags must be nonempty and total at most 450 characters")
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list) or len(raw_sources) > 10:
        raise ValueError("sources must be a list of at most 10 source objects")
    sources = []
    for source in raw_sources:
        if not isinstance(source, dict):
            raise ValueError("Each source must contain title, url, and claim")
        sources.append({
            "title": _text(source.get("title"), "source.title", 300),
            "url": _source_url(source.get("url")),
            "claim": _text(source.get("claim"), "source.claim", 1000),
        })
    chosen_category = _text(category if category is not None else data.get("category", "general knowledge"), "category", 200)
    result = {
        "title": title, "description": description, "tags": tags,
        "fact_text": narration, "category": chosen_category, "slug": slugify(title),
        "word_count": word_count, "sources": sources,
        "review_status": "needs_review",
        "sources_status": "provided_unverified" if sources else "missing",
    }
    if data.get("storyboard") is not None:
        result["storyboard"] = validate_storyboard(data["storyboard"], narration)
    if data.get("generation") is not None:
        generation = data["generation"]
        if not isinstance(generation, dict):
            raise ValueError("generation must be a metadata object")
        provider = _text(generation.get("provider"), "generation.provider", 80)
        model = _text(generation.get("model"), "generation.model", 200)
        created_at = _text(generation.get("created_at"), "generation.created_at", 80)
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError("generation.created_at must be an ISO timestamp with a timezone") from None
        result["generation"] = {"provider": provider, "model": model, "created_at": timestamp.isoformat()}
        if sources:
            result["sources_status"] = "model_supplied_unverified"
    result["quality_warnings"] = _editorial_notes(result)
    return result


def generate_fact(category: str | None = None) -> dict:
    # Keep local script validation and the offline demo independent of requests.
    from pipeline.gemini_client import generate_json

    chosen_category = _text(category if category is not None else random.choice(FACT_CATEGORIES), "category", 200)
    prompt = f"Write one educational short about this topic: {chosen_category}"
    for attempt in range(2):
        raw = generate_json(prompt, schema=FACT_SCHEMA, system_instruction=SYSTEM_PROMPT)
        try:
            # Generate spoken text only once. Imported scripts still use the
            # strict validator, including its exact storyboard/text comparison.
            draft = raw
            if isinstance(raw, dict) and "fact_text" not in raw:
                beats = raw.get("storyboard")
                if (not isinstance(beats, list) or not beats
                        or any(not isinstance(beat, dict)
                               or not isinstance(beat.get("narration"), str)
                               for beat in beats)):
                    raise ValueError("Gemini must return a storyboard with narration for every beat")
                draft = {**raw, "fact_text": " ".join(beat["narration"] for beat in beats)}
            fact = validate_fact(draft, chosen_category)
            if not fact["sources"]:
                raise ValueError("Gemini returned no supporting sources")
            if not fact.get("storyboard"):
                raise ValueError("Gemini returned no storyboard; include beats containing the complete narration in order")
            break
        except ValueError as exc:
            if attempt:
                raise ValueError(
                    f"Gemini returned an invalid script after one correction attempt: {exc}. "
                    "Try a different topic or provide a reviewed script with --script."
                ) from None
            print(f"  Script validation: {exc}; requesting one correction…", flush=True)
            prompt = (
                f"Correct this draft about {chosen_category}. Validation failed: {exc}. "
                "Return the complete JSON object with the required fields. Preserve valid narration "
                "and references. Each source needs a nonempty title, url, and claim. Explain the "
                "claim the reference is expected to support; do not invent supporting evidence "
                "or assert that references have been verified.\nPrevious draft:\n"
                + json.dumps(raw, ensure_ascii=False)[:20000]
            )
    fact["sources_status"] = "model_supplied_unverified"
    fact["generation"] = {
        "provider": "gemini", "model": GEMINI_MODEL,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return validate_fact(fact)


if __name__ == "__main__":
    import json
    print(json.dumps(generate_fact(), indent=2))
