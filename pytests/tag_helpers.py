"""Model doubles for the explicit tag-pair result protocol."""
from types import SimpleNamespace

import json


def pair_response(prompt, decisions=None):
    data = json.loads(prompt.split("\nCorrection:")[0])
    actions = {item["old_tag_id"]: item for item in (decisions or [])}
    results = [{"candidate_id": pair["candidate_id"],
                "decision": actions.get(pair["old_tag_id"], {}).get("decision", "coexist"),
                "reason": actions.get(pair["old_tag_id"], {}).get("reason", "compatible evidence")}
               for pair in data["pairs"]]
    for old_id in set(actions) - {pair["old_tag_id"] for pair in data["pairs"]}:
        results.append({"candidate_id": "foreign", "decision": actions[old_id].get("decision"), "reason": "invalid"})
    return SimpleNamespace(generation_attempts=(), response=json.dumps({"results": results}), prompt_tokens=1, completion_tokens=1, total_tokens=2)
