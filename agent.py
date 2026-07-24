"""
The reasoning core.

RouteIntelligenceAgent runs a tool-use loop: it hands Claude a route and a
date, lets Claude pull event, weather, traffic-baseline, and news data, and
produces one actionable alert. Weather and news are live (Bay Area); events
and traffic are mocked.

The judgment lives in the system prompt. Two rules it insists on:
  1. Compare against the normal baseline for this exact route and time.
  2. Say "all clear" when nothing meaningfully threatens the commute, instead
     of flagging every event or headline it sees.
"""

from __future__ import annotations

import os

import anthropic

import tools

# Model id. Check https://docs.claude.com for the current list; swap freely.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 1500
MAX_TURNS = 6  # safety cap on the tool-use loop

SYSTEM_PROMPT = """\
You are a Predictive Route Intelligence Agent for commuters in the San \
Francisco Bay Area. Given a user's regular travel routine and a specific date, \
you predict whether anything will disrupt that trip and give one clear, \
actionable recommendation ahead of time.

How to reason:
- Use the tools to gather event, weather, traffic-baseline, and news data \
before drawing any conclusion. Do not guess what the data says. Pass the \
route_id you are given to each tool.
- Weather and news are LIVE data. News is unstructured: read each headline and \
decide whether it plausibly affects THIS route corridor. Ignore Bay Area news \
that has nothing to do with the user's path.
- Judge impact relative to a NORMAL day for this exact route and time. Pull the \
traffic baseline so you have something to compare against.
- An event only matters if it plausibly adds load to THIS corridor around the \
user's travel window. A large arena event near the route at departure time \
matters. A small market that is always there does not.
- Weigh timing overlap. A storm that clears before the user leaves is not a threat.
- Be honest about uncertainty. If the live weather shows a clear day, say so; \
do not invent a threat to seem useful.

Output format. End your turn with a short structured alert, exactly:

RISK: <none | low | medium | high>
HEADLINE: <one line, plain language>
WHY: <2-3 sentences citing the specific data that drove this>
RECOMMENDATION: <one concrete action, or "No action needed" when RISK is none>
"""


class RouteIntelligenceAgent:
    def __init__(self, client: anthropic.Anthropic | None = None, verbose: bool = True):
        self.client = client or anthropic.Anthropic()
        self.verbose = verbose

    def _log(self, *args):
        if self.verbose:
            print(*args)

    def analyze(self, routine: dict, date: str) -> str:
        """Run the full loop for one routine on one date and return the alert."""
        user_msg = (
            f"Analyze this routine for {date}.\n\n"
            f"route_id: {routine['route_id']}  (pass this to every tool)\n"
            f"User: {routine['user']}\n"
            f"Trip: {routine['label']}\n"
            f"From: {routine['origin']}\n"
            f"To: {routine['destination']}\n"
            f"Route corridor: {', '.join(routine['corridor'])}\n"
            f"Usual departure: {routine['usual_departure']}\n"
            f"Typical duration: {routine['typical_duration_min']} min\n"
        )

        messages = [{"role": "user", "content": user_msg}]

        for _ in range(MAX_TURNS):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=tools.TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                return self._final_text(resp)

            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    self._log(f"  -> tool: {block.name}({block.input})")
                    result = tools.run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

        return "Reached max reasoning turns without a final alert."

    @staticmethod
    def _final_text(resp) -> str:
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
