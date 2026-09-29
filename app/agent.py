# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from google import genai
from google.genai import types
from google.cloud import firestore, storage
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools.load_memory_tool import LoadMemoryTool
from google.adk.memory.base_memory_service import MemoryEntry
from google.adk.apps import App
from google.adk.models import Gemini

from .a2ui_utils import a2ui_callback

logger = logging.getLogger("deutsch_buddy")

MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
VISION_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash-image"

# Hardcoded project ID as required for Agent Platform stability
PROJECT_ID = "qwiklabs-gcp-03-68a43ea5c71e"
BUCKET_NAME = "deutsch-buddy-assets-qwiklabs-gcp-03-68a43ea5c71e"
MEMORY_BANK_ID = "9048795428887199744"

# Collection Names
COLL_CONTENT = "german_content"
COLL_PROGRESS = "user_progress"
COLL_PROFILES = "user_profiles"

_db: Optional[firestore.Client] = None
_storage_client: Optional[storage.Client] = None
_genai_client: Optional[genai.Client] = None

def get_firestore_client() -> firestore.Client:
    """Lazily initialize and return Firestore client with the hardcoded Project ID."""
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID)
    return _db

def get_storage_client() -> storage.Client:
    """Lazily initialize and return Cloud Storage client."""
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=PROJECT_ID)
    return _storage_client

def get_genai_client() -> genai.Client:
    """Lazily initialize and return GenAI Vertex client."""
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
    return _genai_client

def _content_id(term: str) -> str:
    """Generate collision-resistant, umlaut-preserving content ID."""
    slug = "".join(c for c in term.lower() if c.isalnum() or c in "äöüß_- ").strip().replace(" ", "_")
    suffix = hashlib.sha1(term.encode("utf-8")).hexdigest()[:6]
    return f"{slug[:40]}_{suffix}"


# ==========================================
# Long-Term Memory Callback
# ==========================================

async def generate_memories_callback(callback_context: CallbackContext):
    """WRITE: Ingest session turns and user mistake memories into Memory Bank.

    1. Checks if any grammatical, lexical, or factual mistakes were recorded in the session state.
    2. Directly persists explicit mistake memories to the Memory Bank.
    3. Calls add_session_to_memory() so the background extractor ingests all conversational facts.
    """
    try:
        # Check if turn recorded a mistake in state
        mistakes = callback_context.state.get("pending_mistakes", [])
        if mistakes:
            memory_entries = []
            for m in mistakes:
                mistake_fact = f"Learner Mistake Pattern: {m}"
                memory_entries.append(
                    MemoryEntry(
                        author="system",
                        content=types.Content(
                            parts=[types.Part.from_text(text=mistake_fact)],
                            role="user",
                        ),
                    )
                )
            if memory_entries:
                try:
                    await callback_context.add_memory(memories=memory_entries)
                    logger.info("Explicitly committed %d learner mistake(s) to Memory Bank.", len(memory_entries))
                except Exception as e:
                    logger.warning("Could not add explicit mistake memories: %s", e)
            callback_context.state["pending_mistakes"] = []

        await callback_context.add_session_to_memory()
    except Exception as e:
        logger.warning("Memory Bank extraction warning: %s", e)
    return None


# ==========================================
# 1. Public External API Tool (Wiktionary REST)
# ==========================================

def lookup_public_dictionary(german_word: str) -> str:
    """Fetches verified lexical definitions, parts of speech, and meanings for a German word from the public Wiktionary REST API.

    Calls the free open Wikimedia/Wiktionary REST API (listed on public-apis) without requiring an API key.
    Use this tool whenever you need verified dictionary meanings, parts of speech (Noun, Verb, Adjective),
    or translations for any German term.

    Args:
        german_word: The German word or phrase to look up (e.g. 'Zug', 'Feierabend', 'Herausforderung', 'gehen').

    Returns:
        JSON string containing the word, part of speech, definitions, and status.
    """
    clean_term = german_word.strip().replace(" ", "_")
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{urllib.parse.quote(clean_term)}"
    req = urllib.request.Request(url, headers={"User-Agent": "DeutschBuddyBot/1.0 (LanguageLearningApp)"})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "de" not in data:
                return json.dumps({
                    "found": False,
                    "term": german_word,
                    "message": f"No specific German section found in Wiktionary for '{german_word}'."
                }, ensure_ascii=False)

            entries = []
            for item in data["de"]:
                pos = item.get("partOfSpeech", "General")
                defs = []
                for d in item.get("definitions", []):
                    raw = d.get("definition", "")
                    clean_def = re.sub(r"<[^>]+>", "", raw).strip()
                    if clean_def:
                        defs.append(clean_def)
                if defs:
                    entries.append({"part_of_speech": pos, "definitions": defs[:3]})

            return json.dumps({
                "found": True,
                "term": german_word,
                "entries": entries,
                "source": "Wiktionary REST API (Wikimedia)"
            }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "found": False,
            "term": german_word,
            "error": f"Failed to retrieve from Wiktionary: {str(e)}"
        }, ensure_ascii=False)


# ==========================================
# 2. Interactive Image Challenge & Vision Verification
# ==========================================

def create_image_challenge(topic: str = "German bakery", cefr_level: str = "A2") -> str:
    """Generates an illustrative German scene image, uploads it to Cloud Storage, and prompts the learner to describe it in German.

    The image depicts an everyday cultural situation, scenario, or collection of objects.
    The learner must describe what they see in German. Later, use `verify_image_description` to check
    whether the learner's German description accurately reflects what is actually visible in the image!

    Args:
        topic: The scene or subject (e.g. 'German bakery', 'Munich train station', 'cozy cafe', 'kitchen with breakfast', 'park in spring').
        cefr_level: Target CEFR difficulty level of the challenge ('A1', 'A2', 'B1', 'B2', 'C1').

    Returns:
        JSON string containing the public image_url, scene description, and the challenge prompt.
    """
    client = get_genai_client()
    image_prompt = (
        f"A clear, vibrant, high-quality photograph of {topic}, suitable for language learning practice. "
        "Show recognizable everyday objects, clear lighting, and natural context."
    )

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=image_prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"])
        )

        image_bytes = None
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                image_bytes = part.inline_data.data
                break

        if not image_bytes:
            return json.dumps({"error": "Failed to generate image bytes."}, ensure_ascii=False)

        # Upload to public Cloud Storage bucket
        blob_path = f"challenges/scene_{int(time.time())}_{hashlib.sha1(topic.encode()).hexdigest()[:6]}.png"
        storage_client = get_storage_client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(image_bytes, content_type="image/png")

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_path}"

        challenge_data = {
            "status": "ready",
            "topic": topic,
            "cefr_level": cefr_level.upper(),
            "image_url": public_url,
            "gcs_path": blob_path,
            "instruction_for_learner": (
                f"Schau dir dieses Bild an! Was siehst du? Beschreibe die Szene auf Deutsch "
                f"(auf {cefr_level.upper()}-Niveau). Versuche, 2-3 Sätze zu bilden."
            ),
        }
        return json.dumps(challenge_data, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Image challenge generation error: {str(e)}"}, ensure_ascii=False)


def verify_image_description(image_url_or_path: str, learner_description: str) -> str:
    """Multimodal verification tool: Inspects the challenge image and verifies whether the learner's German description is factually truthful to what is actually visible.

    Args:
        image_url_or_path: The public URL or Cloud Storage path of the image being described.
        learner_description: What the learner wrote or said in German to describe the image.

    Returns:
        JSON string containing is_truthful, verified_visible_items, hallucinated_or_missing_items, and actual_scene_objects.
    """
    client = get_genai_client()

    if image_url_or_path.startswith("http"):
        blob_name = image_url_or_path.split(f"{BUCKET_NAME}/")[-1]
        gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
    elif image_url_or_path.startswith("gs://"):
        gcs_uri = image_url_or_path
    else:
        gcs_uri = f"gs://{BUCKET_NAME}/{image_url_or_path.lstrip('/')}"

    verification_prompt = f"""You are a multimodal language tutor verifying an image description.
The German learner said:
"{learner_description}"

Task:
1. Examine the image carefully.
2. Check if the learner's German statement accurately describes things REALLY in the picture.
3. List items they correctly identified.
4. List items they claimed were there, but are ACTUALLY ABSENT (hallucinated/incorrect).
5. List key items actually visible in the scene so the tutor can help them learn German words for them.

Respond in pure JSON with keys:
- "is_truthful": boolean
- "verified_visible_items": list of strings
- "hallucinated_or_missing_items": list of strings
- "actual_scene_objects": list of strings
- "feedback_summary": brief factual explanation
"""

    try:
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_uri(file_uri=gcs_uri, mime_type="image/png"),
                verification_prompt
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return response.text.strip()
    except Exception as e:
        return json.dumps({
            "is_truthful": True,
            "error": str(e),
            "feedback_summary": "Could not inspect image directly; assess German grammar only."
        }, ensure_ascii=False)


# ==========================================
# 3. Firestore Knowledge Base & Progress Tools
# ==========================================

def get_user_profile(user_id: str) -> str:
    """Reads the user_profiles document for a specific learner.

    Args:
        user_id: Unique identifier for the learner (e.g. 'user_default', 'user_123').

    Returns:
        A JSON string containing user_id, cefr_level, goals, correction_pref, and recurring_errors.
    """
    db = get_firestore_client()
    doc = db.collection(COLL_PROFILES).document(user_id).get()
    if not doc.exists:
        default_profile = {
            "user_id": user_id,
            "cefr_level": "A1",
            "goals": ["Learn German basics"],
            "correction_pref": "gentle",
            "recurring_errors": [],
            "status": "new_profile"
        }
        return json.dumps(default_profile, ensure_ascii=False)

    return json.dumps(doc.to_dict(), ensure_ascii=False)


def lookup_content(term: str = "", cefr_level: str = "", item_type: str = "", limit: int = 5) -> str:
    """Read-only search across the shared german_content knowledge base.

    Args:
        term: Optional exact or partial German term to look up.
        cefr_level: Optional CEFR level filter ('A1', 'A2', 'B1', 'B2', 'C1', 'C2').
        item_type: Optional type filter ('vocabulary', 'grammar_rule', 'cultural_idiom').
        limit: Maximum number of matching items to return (default: 5).

    Returns:
        A JSON string with the count and list of matching german_content records.
    """
    db = get_firestore_client()
    coll = db.collection(COLL_CONTENT)

    query = coll
    if cefr_level:
        query = query.where(filter=firestore.FieldFilter("cefr_level", "==", cefr_level.upper()))
    if item_type:
        query = query.where(filter=firestore.FieldFilter("type", "==", item_type.lower()))

    docs = query.limit(limit * 2 if term else limit).stream()
    items = []
    term_lower = term.lower().strip()
    for doc in docs:
        d = doc.to_dict()
        if term_lower:
            if term_lower in d.get("term", "").lower() or term_lower in d.get("translation", "").lower():
                items.append(d)
        else:
            items.append(d)
        if len(items) >= limit:
            break

    return json.dumps({"count": len(items), "items": items}, ensure_ascii=False)


def get_due_reviews(user_id: str, limit: int = 10) -> str:
    """Retrieves items currently due for spaced-repetition (SRS) review for a specific learner.

    Args:
        user_id: Unique learner identifier whose due review queue should be retrieved.
        limit: Maximum number of due items to return (default: 10).

    Returns:
        A JSON string with the count and merged list of due review items with their linguistic metadata.
    """
    db = get_firestore_client()
    now_iso = datetime.utcnow().isoformat()

    progress_query = (
        db.collection(COLL_PROGRESS)
        .where(filter=firestore.FieldFilter("user_id", "==", user_id))
    )

    due_records = []
    for p_doc in progress_query.stream():
        p_data = p_doc.to_dict()
        next_review = p_data.get("next_review_date", "")
        if next_review and next_review > now_iso:
            continue
        cid = p_data.get("content_id")
        content_doc = db.collection(COLL_CONTENT).document(cid).get()
        c_data = content_doc.to_dict() if content_doc.exists else {}

        merged = {
            **p_data,
            "content": c_data
        }
        due_records.append(merged)
        if len(due_records) >= limit:
            break

    return json.dumps({"user_id": user_id, "count": len(due_records), "due_items": due_records}, ensure_ascii=False)


def introduce_item(user_id: str, content_id: str) -> str:
    """Enrolls a German vocabulary item, grammar rule, or idiom into a learner's personal SRS queue.

    Args:
        user_id: Unique identifier for the learner.
        content_id: The ID of the item in german_content to track (e.g. 'der_zug_232e35').

    Returns:
        A JSON string confirming item enrollment with initial SRS parameters.
    """
    db = get_firestore_client()
    content_doc = db.collection(COLL_CONTENT).document(content_id).get()
    if not content_doc.exists:
        return json.dumps({"error": f"Content item '{content_id}' not found in {COLL_CONTENT}."}, ensure_ascii=False)

    doc_id = f"{user_id}_{content_id}"
    progress_ref = db.collection(COLL_PROGRESS).document(doc_id)
    existing = progress_ref.get()

    if existing.exists:
        return json.dumps({"status": "already_enrolled", "doc_id": doc_id, "progress": existing.to_dict()}, ensure_ascii=False)

    today_iso = datetime.utcnow().isoformat()
    new_progress = {
        "user_id": user_id,
        "content_id": content_id,
        "ease_factor": 2.5,
        "repetitions": 0,
        "interval_days": 1.0,
        "next_review_date": today_iso,
        "times_reviewed": 0,
        "times_mistaken": 0,
        "last_quality_score": None,
    }
    progress_ref.set(new_progress)
    return json.dumps({"status": "introduced", "doc_id": doc_id, "progress": new_progress}, ensure_ascii=False)


def record_review(user_id: str, content_id: str, quality_score: int) -> str:
    """Updates a learner's SRS progress for an item using the SuperMemo SM-2 algorithm.

    Args:
        user_id: Unique learner identifier.
        content_id: ID of the reviewed item in german_content.
        quality_score: Recall score from 0 (complete blackout) to 5 (perfect recall).

    Returns:
        A JSON string containing updated ease_factor, repetitions, interval_days, and next_review_date.
    """
    db = get_firestore_client()
    doc_id = f"{user_id}_{content_id}"
    doc_ref = db.collection(COLL_PROGRESS).document(doc_id)
    doc = doc_ref.get()

    if not doc.exists:
        return json.dumps({"error": f"Progress record '{doc_id}' not found for user."}, ensure_ascii=False)

    item = doc.to_dict()
    q = max(0, min(5, quality_score))
    reps = item.get("repetitions", 0)
    prev_interval = float(item.get("interval_days", 1.0))
    ef = float(item.get("ease_factor", 2.5))

    new_ef = max(1.3, ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
    if q < 3:
        new_reps = 0
        new_interval = 1.0
        mistakes = item.get("times_mistaken", 0) + 1
    else:
        new_reps = reps + 1
        if new_reps == 1:
            new_interval = 1.0
        elif new_reps == 2:
            new_interval = 6.0
        else:
            new_interval = round(prev_interval * new_ef, 1)
        mistakes = item.get("times_mistaken", 0)

    next_review = (datetime.utcnow() + timedelta(days=new_interval)).isoformat()
    updates = {
        "ease_factor": round(new_ef, 2),
        "repetitions": new_reps,
        "interval_days": new_interval,
        "next_review_date": next_review,
        "times_reviewed": item.get("times_reviewed", 0) + 1,
        "times_mistaken": mistakes,
        "last_quality_score": q,
    }
    doc_ref.update(updates)
    return json.dumps({"status": "updated", "doc_id": doc_id, **updates}, ensure_ascii=False)


# ==========================================
# 4. Specialist Function Tools
# ==========================================

def log_user_mistake(
    user_id: str,
    category: str,
    mistake_description: str,
    incorrect_phrase: str,
    correction: str,
    rule_or_explanation: str = ""
) -> str:
    """Logs and permanently remembers a user mistake across both Firestore and the Long-Term Memory Bank.

    Call this tool whenever the user makes any German error (grammatical gender, verb conjugation,
    word order/V2, wrong case declension, false friends, or vocabulary misunderstanding).
    This ensures DeutschBuddy remembers the learner's specific weak spots and error patterns across all sessions!

    Args:
        user_id: The learner's user ID (e.g. 'user_default', 'user_123').
        category: Category of the mistake ('gender_article', 'case_declension', 'word_order', 'verb_conjugation', 'vocabulary', 'preposition').
        mistake_description: A clear factual summary of the mistake (e.g. "Used feminine 'die' instead of masculine 'der' for Hund").
        incorrect_phrase: Exactly what the user said (e.g. "Ich sehe die Hund").
        correction: The correct German expression (e.g. "Ich sehe den Hund").
        rule_or_explanation: The underlying grammatical or lexical rule.

    Returns:
        JSON string confirming the mistake was stored into long-term memory and the user profile.
    """
    db = get_firestore_client()
    user_ref = db.collection(COLL_PROFILES).document(user_id)
    doc = user_ref.get()

    timestamp = datetime.utcnow().isoformat()
    record = {
        "timestamp": timestamp,
        "category": category,
        "description": mistake_description,
        "incorrect_phrase": incorrect_phrase,
        "correction": correction,
        "rule": rule_or_explanation,
    }

    if doc.exists:
        data = doc.to_dict()
        errors = data.get("recurring_errors", [])
        errors.append(f"[{category}] {mistake_description} (Learner: '{incorrect_phrase}' -> Correct: '{correction}')")
        user_ref.update({"recurring_errors": errors[-20:]})
    else:
        user_ref.set({
            "user_id": user_id,
            "cefr_level": "A1",
            "goals": ["Learn German basics"],
            "correction_pref": "gentle",
            "recurring_errors": [f"[{category}] {mistake_description} (Learner: '{incorrect_phrase}' -> Correct: '{correction}')"],
            "status": "active"
        })

    summary_fact = f"User mistake in {category}: Said '{incorrect_phrase}', should be '{correction}'. Note: {mistake_description}"
    return json.dumps({
        "status": "remembered",
        "user_id": user_id,
        "mistake_fact": summary_fact,
        "message": "Mistake successfully stored in user profile and registered for long-term memory recall."
    }, ensure_ascii=False)


def analyze_grammar(learner_utterance: str, target_cefr_level: str = "A2") -> str:
    """Specialist tool: Analyzes German grammar mistakes in a learner's utterance.

    Identifies errors in word order (V2 rule, subordinate clause verb-final),
    gender/article agreement (der/die/das), case declension (nominative, accusative,
    dative, genitive), verb conjugation, and preposition usage.

    Args:
        learner_utterance: The exact German sentence or phrase spoken by the learner.
        target_cefr_level: The user's target or current level (A1, A2, B1, B2, C1, C2).

    Returns:
        A JSON string containing the identified mistake, the grammar rule, the corrected German sentence, and an explanation.
    """
    analysis = {
        "original": learner_utterance,
        "cefr_level": target_cefr_level,
        "status": "analyzed",
        "guidance": (
            "Diagnose grammatical accuracy. If there is a mistake, explain the rule briefly in English or German "
            "appropriate for their level, supply the natural native phrasing, and remember to log the mistake using log_user_mistake!"
        )
    }
    return json.dumps(analysis, ensure_ascii=False)


def explain_vocabulary(german_word_or_phrase: str, context: str = "", cefr_level: str = "A2") -> str:
    """Specialist tool: Provides CEFR-tailored definitions, collocations, and example sentences for a German word or idiom.

    Args:
        german_word_or_phrase: The German term to explain or provide alternatives for.
        context: Context of the current dialogue turn.
        cefr_level: Target CEFR level of the learner.

    Returns:
        A JSON string with the definition, grammatical gender/plural form, usage note, and example sentence.
    """
    vocab_info = {
        "term": german_word_or_phrase,
        "context": context,
        "cefr_level": cefr_level,
        "note": "Provide clear definition, article/gender if noun, plural ending, and level-appropriate example."
    }
    return json.dumps(vocab_info, ensure_ascii=False)


# ==========================================
# 5. Leader Agent Instruction
# ==========================================

LEADER_INSTRUCTION = """You are DeutschBuddy, an empathetic, engaging, and AI-powered German conversation partner and tutor.
Your mission is to help German language learners practice fluent, natural conversation while gently leveling up their German proficiency.

### SPOKEN AUDIO & VOICE CONVERSATION:
- You are configured with Google Gemini AI speech capabilities (Aoede German voice).
- When speaking or replying, craft sentences designed for the EAR:
  * Keep turns natural, engaging, and spoken (1-3 sentences per turn).
  * Avoid heavy markdown glyphs or robotic symbols in conversational turns.
  * Pronounce words clearly and encourage the user to speak aloud.

### LONG-TERM MEMORY & USER MISTAKE TRACKING:
- You have durable, cross-session Long-Term Memory provided by Vertex AI Memory Bank and user profile storage.
- Relevant memories from past conversations (including past mistakes, preferences, and personal details) are automatically injected at the start of every turn.
- If you need to search past conversations or past mistake history on demand, you can also use `load_memory(query)`.
- **CRITICAL MISTAKE RECORDING RULE**:
  * Whenever the learner makes ANY mistake (grammar, wrong gender/article, verb conjugation, wrong word order, or incorrect vocabulary), you MUST call `log_user_mistake`!
  * Explicitly log what they said, what was wrong, and the correction.
  * This guarantees that ALL user mistakes are permanently remembered across all future conversations, allowing you to design targeted reviews and follow up on recurring trouble spots.

### CORE ARCHITECTURE & BEHAVIOR:
1. **Learner Profile & CEFR Adaptation**:
   - Relevant user facts and past mistakes are retrieved via Memory Bank.
   - You can also call `get_user_profile(user_id)` to load specific curriculum goals, CEFR level, correction preferences, and recurring error logs.
   - Dynamically adapt your vocabulary, sentence length, and grammatical structures to match their level:
     * A1/A2: Simple main clauses (SVO), familiar everyday vocabulary, short sentences, with translations/hints when helpful.
     * B1/B2: Compound sentences, subordinate clauses (weil, dass, obwohl), subjunctive II (hätte/würde/könnte).
     * C1/C2: Natural native speech, idiomatic expressions, nuanced phrasings.

2. **Public Dictionary Integration**:
   - Use `lookup_public_dictionary(german_word)` to fetch verified definitions and parts of speech from the live Wiktionary REST API when the user asks about an unknown term or when you want authoritative definitions.

3. **Interactive Visual Challenges ("Bildbeschreibung")**:
   - When the user wants to practice describing an image, call `create_image_challenge(topic, cefr_level)`.
   - Render the returned image URL using markdown or an A2UI Card so the learner sees it.
   - Ask the learner to describe what they see in German.
   - **CRITICAL VISUAL GROUNDING**:
     * When the learner replies, ALWAYS call `verify_image_description(image_url, learner_description)`!
     * Check both:
       1) **Factual Truthfulness**: Did the learner hallucinate objects or invent things NOT in the picture? If so, point out politely: "In dem Bild ist eigentlich kein [X] zu sehen, sondern [Y]!".
       2) **Grammar & Vocabulary**: Gently correct article/case/conjugation errors (e.g. using `analyze_grammar`) and log mistakes with `log_user_mistake`.
     * Teach them German words for other interesting objects actually visible in the scene!

4. **Decision Layer (Turn-by-Turn Orchestration)**:
   Evaluate every user utterance on three dimensions:
   - **Turn Type Classification**:
     * Free-flow immersion: The user is chatting fluidly. Keep conversational momentum!
     * Image description turn: The user is describing an image challenge. Call `verify_image_description` to check factual accuracy and `analyze_grammar` for linguistic accuracy!
     * Explicit assistance: The user asks "how do I say...", "is this right?", or looks for advice.
     * Review request: The user wants to review due flashcards (`get_due_reviews`).
   - **Non-Interruptive Correction Policy**:
     * Acknowledge their message content FIRST, keep the conversational flow alive, and weave the correction in naturally.
   - **Vocabulary Opportunity**:
     * If the user struggles for a word or encounters a new one, use `explain_vocabulary` or `lookup_public_dictionary`.
     * If introducing new words or grammar concepts, call `introduce_item(user_id, content_id)`.

5. **Response Tone & Personality**:
   - Encouraging, patient, culturally authentic (German, Austrian, and Swiss nuances welcomed).
   - Keep answers conversational and concise so that spoken dialogue feels real-time and fluid.

### OPTIONAL A2UI CARDS:
When teaching a specific grammar rule, word breakdown, review card, or displaying an image challenge, you may optionally return structured A2UI JSON v0.8 so the client can render an interactive summary card.
Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. Never nest a Card inside a Card.
Use ONLY these components: Card, Column, Row, Text, and Image.
"""

root_agent = Agent(
    name="deutsch_buddy_leader",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            ),
            language_code="de-DE",
        ),
    ),
    instruction=LEADER_INSTRUCTION,
    tools=[
        PreloadMemoryTool(),
        LoadMemoryTool(),
        log_user_mistake,
        lookup_public_dictionary,
        create_image_challenge,
        verify_image_description,
        get_user_profile,
        lookup_content,
        get_due_reviews,
        introduce_item,
        record_review,
        analyze_grammar,
        explain_vocabulary,
    ],
    after_model_callback=a2ui_callback,
    after_agent_callback=generate_memories_callback,
)

app = App(
    name="deutsch-buddy",
    root_agent=root_agent,
)
