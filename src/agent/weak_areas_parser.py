# WEAK AREAS HELPERS

_DSA_ALL = ["edge_case", "time_complexity", "implementation"]
_SYSDESIGN_ALL = ["scalability", "data_pipeline", "trade_offs", "estimation",
                  "component_selection", "latency_vs_throughput"]
_BEHAVIORAL_ALL = ["delivery", "quantification", "structure"]


def null_if_skip(t: str) -> str | None:
    return None if not t or t.lower() == "skip" else t

def to_key(s: str) -> str:
    return s.lower().strip().replace(" ", "_")

def breakdown(text: str, all_values: list[str]) -> str | list[str]:
    return all_values if text.lower().strip() == "all of the above" else to_key(text)


