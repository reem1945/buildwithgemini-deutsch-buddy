"""Seed Firestore collections with split collections:
- german_content: immutable linguistic knowledge base
- user_progress: per-learner SRS tracking
- user_profiles: per-learner preferences and goals
"""

import hashlib
from datetime import datetime
from google.cloud import firestore

PROJECT_ID = "qwiklabs-gcp-03-68a43ea5c71e"

def _content_id(term: str) -> str:
    """Generate collision-resistant, umlaut-preserving content ID."""
    slug = "".join(c for c in term.lower() if c.isalnum() or c in "äöüß_- ").strip().replace(" ", "_")
    suffix = hashlib.sha1(term.encode("utf-8")).hexdigest()[:6]
    return f"{slug[:40]}_{suffix}"

def seed():
    db = firestore.Client(project=PROJECT_ID)

    # 1. Linguistic content (shared knowledge base)
    content_items = [
        {
            "term": "der Zug",
            "type": "vocabulary",
            "translation": "the train",
            "cefr_level": "A1",
            "gender": "masculine",
            "plural": "die Züge",
            "example_sentence": "Ich fahre morgen mit dem Zug nach München.",
            "example_translation": "I am traveling to Munich tomorrow by train.",
            "grammar_notes": "Takes dative after preposition 'mit' (mit dem Zug).",
        },
        {
            "term": "die Herausforderung",
            "type": "vocabulary",
            "translation": "the challenge",
            "cefr_level": "B1",
            "gender": "feminine",
            "plural": "die Herausforderungen",
            "example_sentence": "Die deutsche Grammatik ist eine echte Herausforderung.",
            "example_translation": "German grammar is a real challenge.",
            "grammar_notes": "Noun ending in -ung is always feminine (die).",
        },
        {
            "term": "Kausalsatz mit 'weil'",
            "type": "grammar_rule",
            "translation": "Subordinate clause with 'because' (verb-final)",
            "cefr_level": "A2",
            "gender": None,
            "plural": None,
            "example_sentence": "Ich lerne Deutsch, weil ich in Berlin arbeiten will.",
            "example_translation": "I am learning German because I want to work in Berlin.",
            "grammar_notes": "'weil' sends the conjugated verb to the very end of the subclause.",
        },
        {
            "term": "Wechselpräpositionen",
            "type": "grammar_rule",
            "translation": "Two-way prepositions (Akkusativ vs. Dativ)",
            "cefr_level": "B1",
            "gender": None,
            "plural": None,
            "example_sentence": "Ich gehe in das (ins) Kino [Wohin? Akkusativ]. Ich bin im Kino [Wo? Dativ].",
            "example_translation": "I go into the cinema (direction). I am in the cinema (location).",
            "grammar_notes": "Movement/direction = Akkusativ. Position/location = Dativ.",
        },
        {
            "term": "der Feierabend",
            "type": "cultural_idiom",
            "translation": "end of the working day / evening free time",
            "cefr_level": "A2",
            "gender": "masculine",
            "plural": "die Feierabende",
            "example_sentence": "Schönen Feierabend! Bis morgen!",
            "example_translation": "Have a nice evening after work! See you tomorrow!",
            "grammar_notes": "Common German cultural concept. 'Feierabend machen' = to call it a day.",
        }
    ]

    content_coll = db.collection("german_content")
    seeded_content_ids = []
    for item in content_items:
        cid = _content_id(item["term"])
        doc_data = {"id": cid, **item}
        content_coll.document(cid).set(doc_data)
        seeded_content_ids.append(cid)
        print(f"✓ Seeded german_content: {cid} ({item['term']})")

    # 2. Seed a sample user profile
    sample_user_id = "user_default"
    user_profile = {
        "user_id": sample_user_id,
        "cefr_level": "A2",
        "goals": ["Everyday conversation in Berlin", "Ordering food & drinks", "B1 Certification"],
        "correction_pref": "gentle",
        "recurring_errors": ["Dativ vs Akkusativ with two-way prepositions", "Verb position after 'weil'"],
    }
    db.collection("user_profiles").document(sample_user_id).set(user_profile)
    print(f"✓ Seeded user_profiles: {sample_user_id}")

    # 3. Seed user progress for the sample user (der Zug and Kausalsatz mit 'weil' due today)
    today_iso = datetime.utcnow().isoformat()
    progress_items = [
        {
            "user_id": sample_user_id,
            "content_id": seeded_content_ids[0],  # der Zug
            "ease_factor": 2.5,
            "repetitions": 1,
            "interval_days": 1.0,
            "next_review_date": today_iso,
            "times_reviewed": 1,
            "times_mistaken": 0,
            "last_quality_score": 4,
        },
        {
            "user_id": sample_user_id,
            "content_id": seeded_content_ids[2],  # weil subordinate
            "ease_factor": 2.4,
            "repetitions": 2,
            "interval_days": 1.0,
            "next_review_date": today_iso,
            "times_reviewed": 2,
            "times_mistaken": 1,
            "last_quality_score": 3,
        }
    ]

    for p in progress_items:
        doc_id = f"{p['user_id']}_{p['content_id']}"
        db.collection("user_progress").document(doc_id).set(p)
        print(f"✓ Seeded user_progress: {doc_id}")

    print(f"\nDone! Seeded collections 'german_content', 'user_profiles', and 'user_progress' in project '{PROJECT_ID}'.")

if __name__ == "__main__":
    seed()
