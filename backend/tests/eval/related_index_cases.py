"""Fictional sources and separately held scoring labels for #2728.

Only SOURCES are passed to ingestion/Sleep. Never send PROBES to the builder.
"""

SOURCES = {
    "recommendation": "On 2024-03-04, Neri recommended the Luma workshop to Tavi for guitar repairs.",
    "repair": "On 2024-03-05, Tavi had a guitar repaired at the Luma workshop.",
    "address": "The Luma workshop is located on Pine Street in the fictional town of Oru.",
    "distractor": "Mira recommended the Sola salon to Neri for a haircut on 2024-03-06.",
    "ferry-old": "The Oru ferry departure was Tuesday at 09:00. This schedule was published on 2024-02-01.",
    "ferry-new": "Effective 2024-03-01, the Oru ferry departs Friday at 11:00, replacing the Tuesday 09:00 schedule.",
    "library": "The Oru library opens at 09:00 on Tuesday. Its schedule did not change in March.",
    "parcel": "On 2024-03-04, Neri sent a parcel to Tavi using the Runo courier.",
}

PROBES = (
    {
        "id": "single",
        "fields": ["recommender"],
        "query": "Who recommended the guitar repair workshop to Tavi?",
        "facts": {"recommender": "Neri"},
        "sources": ["recommendation"],
    },
    {
        "id": "cross",
        "fields": ["workshop", "recommender"],
        "query": "Where did Tavi get the guitar repaired, and who recommended that workshop?",
        "facts": {"workshop": "Luma", "recommender": "Neri"},
        "sources": ["recommendation", "repair"],
    },
    {
        "id": "location",
        "fields": ["street"],
        "query": "On which street is the workshop that repaired Tavi's guitar?",
        "facts": {"street": "Pine Street"},
        "sources": ["repair", "address"],
    },
    {
        "id": "dates",
        "fields": ["recommended_on", "repaired_on"],
        "query": "When was the guitar workshop recommended to Tavi and when was the guitar repaired? Use ISO dates.",
        "facts": {"recommended_on": "2024-03-04", "repaired_on": "2024-03-05"},
        "sources": ["recommendation", "repair"],
    },
    {
        "id": "direction",
        "fields": ["recommender", "salon"],
        "query": "Who recommended a salon to Neri, and what was it called?",
        "facts": {"recommender": "Mira", "salon": "Sola"},
        "sources": ["distractor"],
    },
    {
        "id": "update",
        "fields": ["weekday", "time"],
        "query": "What weekday and time does the Oru ferry currently depart?",
        "facts": {"weekday": "Friday", "time": "11:00"},
        "sources": ["ferry-new"],
    },
    {
        "id": "distractor",
        "fields": ["time"],
        "query": "What time does the Oru library open on Tuesday?",
        "facts": {"time": "09:00"},
        "sources": ["library"],
    },
    {
        "id": "unanswerable",
        "fields": ["amount"],
        "query": "How much did Tavi pay for the guitar repair?",
        "facts": {},
        "sources": [],
    },
)
