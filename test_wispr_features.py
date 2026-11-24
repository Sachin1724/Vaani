"""Snippets, learn-from-edits and the AI polish guard. Run: python test_wispr_features.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.formatter import expand_snippets, find_corrections
from engine.local_llm import accept_polish

S = [{"phrase_match": "my address", "replace_with": "12 MG Road, Bhubaneswar"}]
assert expand_snippets("My address.", S) == "12 MG Road, Bhubaneswar"
assert expand_snippets("Send it to my address today.", S) == "Send it to 12 MG Road, Bhubaneswar today."
assert expand_snippets("No trigger here.", S) == "No trigger here."

assert find_corrections("I met Preeya at the open flow office today.",
                        "Notes: I met Priya at the OpenFlow office today. Bye") == [("Preeya", "Priya")]
assert find_corrections("Please call him now ok", "Totally different text written here instead") == []
assert find_corrections("Hi", "Hello") == []  # too short to align

o = "i meet priya at the open flow office tomorrow at ten"
assert accept_polish(o, "Sure! What time works best for your meeting with Priya?") == o
assert accept_polish(o, "I meet Priya at the OpenFlow office tomorrow at ten.") != o
print("wispr features ok")
