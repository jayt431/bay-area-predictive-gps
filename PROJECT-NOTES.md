# Project Notes — Bay Area Predictive GPS

**A plain-English companion to this project.** `CLAUDE.md` is the technical
reference written for AI agents working on the code. This file is written for
*you*: what the project is, how it actually works, what went wrong and how it
was diagnosed, and how to explain any of it to another person.

Upload this file into a Claude conversation and ask questions about anything in
it. It is meant to be talked through, not just read.

**Last updated:** 2026-10-08 · **Live:** https://bay-area-predictive-gps.onrender.com ·
**Repo:** github.com/jayt431/bay-area-predictive-gps

---

## 1. What this project is

A GPS that tells you about problems on your route **before you leave**, instead
of while you are already stuck in them.

Google Maps and Waze are *reactive*: they notice traffic once it exists. The
premise here is that most disruption is knowable in advance — a concert lets out
at 10pm, construction is scheduled weeks ahead, a protest has a permit, rain is
forecast. If the app knows your trip in advance, it can warn you the evening
before.

The long-term goal is global. The Bay Area is the first region because it is
local, easy to verify by eye, and has unusually good open data.

---

## 2. How it works, end to end

Follow one trip through the system:

1. **You pick a destination.** The browser asks Mapbox to turn "Ferry Building"
   into coordinates, then asks Mapbox Directions for a driving route.
2. **The browser checks the route against a pool of known disruptions.** The
   server hands it every active disruption in the region; the browser measures
   each one's distance from the route line. Within 0.5 km is red, within 2 km is
   yellow, anything further is ignored.
3. **Parking is looked up at the destination** — real metered street locations
   from San Francisco's open data, plus parking garages read out of the map's own
   tile data.
4. **The assembled trip is sent to the server** (`POST /api/brief`), which adds
   live weather for the destination and recent local headlines, then produces one
   alert: a risk level, a headline, the reasoning, and one recommendation.
5. **If the trip is on your schedule**, a nightly job does all of the above
   without a browser open and emails you the result.

The important structural idea: **every data source is normalized to the same
shape.** Swapping a fake source for a real one means rewriting one function's
body and nothing else.

---

## 3. What is real and what is invented

Being able to say this precisely is worth more than claiming everything is live.

| Source | Status | Where it comes from |
|--------|--------|---------------------|
| Routing, geocoding, map tiles | **Real** | Mapbox |
| Weather | **Real** | OpenWeather 5-day/3-hour forecast |
| Local news headlines | **Real** | NewsAPI |
| Metered street parking | **Real** | DataSF (every SFMTA meter) |
| Parking garages | **Real** | Read from Mapbox map tiles in the browser |
| Blocked streets / civic events | **Real** | San Francisco 311 |
| Highway incidents & construction | **Real** | 511 SF Bay (Open511), live in production |
| Parking availability | **Invented** | Predicted, not measured — no live curb feed exists |
| Break-in risk | **Invented** | Placeholder; real path is SFPD incident data |
| Events (concerts, games) | **Invented** | Real path is Ticketmaster |
| Traffic baseline | **Invented** | Real path is Google Maps Routes API |
| The AI trip brief | **Off by choice** | Needs a paid Anthropic API key; see §7 |

Everything degrades rather than breaking. A missing key removes one feature; it
never takes down the app.

---

## 4. What got built, and why

In the order it happened.

### Live weather and news reached the brief
The two genuinely live sources were only wired into an older command-line
version, never into the map people actually use. The brief was reasoning over
disruptions and parking alone. **Why it mattered:** the whole pitch is synthesis
across sources, and the user-facing surface was synthesizing two of them.

A follow-up fixed the forecast location: it was reading weather at *home* rather
than the destination. Over a cross-city trip in San Francisco those genuinely
differ — fog in the Sunset, sun in the Mission.

### Real highway disruptions (511 SF Bay)
The red/yellow route alerts — the app's headline feature — were entirely
invented. 511.org publishes real incidents and construction for all nine Bay
Area counties.

**The design constraint that shaped it:** 511 allows only 60 requests per hour.
But one request returns the *whole region*, and the browser already filters by
route. So the server fetches once every two minutes and serves every visitor
from that cached copy — 30 requests an hour, half the limit, regardless of how
many people use the site.

**The insight worth repeating to someone:** 511.org implements **Open511**, an
open standard. The same code works against 511NY, 511GA, Ontario, Idaho, Alaska,
and British Columbia with a different address and key. This was not an
integration with one city — it was an integration with a protocol, which is what
makes the global ambition plausible.

### The scheduler
The app only knew about a trip when you asked for a route. The scheduler is what
makes the actual promise possible: knowing the trip in advance so the warning can
arrive beforehand.

Two ways in. **Manual:** pick a destination, a time, and which weekdays it
repeats. **Calendar:** paste the secret calendar link that Google, Apple, and
Outlook all publish, and events with a location become scheduled trips.

**The panel is a month calendar.** The first version was a single form with day
buttons *and* a date box, where picking a day silently greyed the date out —
"repeats weekly" and "happens once" were two modes jammed into one form with no
visible switch, and it read as broken. It was rebuilt the way Google and Apple
Calendar already work, so there is nothing new to learn: a month grid with a dot
on each day that has a trip (teal for ones you added, blue for imported), click
a day to see it, and "+ Add trip" opens a form that asks *where*, a date, an
"arrive by" time, and a **Repeat** dropdown — doesn't repeat, weekly, every
weekday, or custom days. The place is searched inside the form, so you no longer
have to route to it on the map first.

The grid is drawn entirely in the browser from the trips the server already
returns, so this needed no database change. One consequence: a repeating trip
has no start date stored, so it is drawn from today forward. Calendar imports
now look about two months ahead instead of two weeks, so the month view is not
empty past the first fortnight.

See §6 for why calendars work this way rather than through "sign in with
Google."

### Privacy, persistence, and plumbing
Three fixes that came out of deploying it:

- **A passcode on the schedule.** The map is a public demo, but a schedule is a
  record of where you go and when you are not home.
- **A real database.** Alerts must work with the app closed, so the schedule
  cannot live in the browser.
- **One database connection per request** instead of one per call — see §5.

---

## 5. Problems hit, and how they were found

This section is the most useful one to be able to talk about. Anyone can list
features; being able to describe a diagnosis is what demonstrates you understood
the system.

### Parking silently stopped working
**Symptom:** clicking "Show on map" did nothing.

**Cause:** DataSF moved from `data.sfgov.org` to `data.sf.gov`. The old address
still redirects, but a redirected request carrying a `$select` parameter comes
back `403 Forbidden` — and `$select` is exactly what the meter query uses. It was
isolated by testing each query parameter separately: `$where` alone worked,
`$where` + `$limit` worked, `$where` + `$select` failed, five times out of five.

**The deeper lesson:** the frontend never checked for an error, so a dead backend
looked like a dead button. The fix was two parts — the new address, *and* making
the UI say which of two very different things happened: "this feed is down"
versus "there is genuinely no parking here."

### Then it still didn't work — for a completely different reason
The next test routed to **SFO**, which is in San Mateo County. SFMTA meters cover
San Francisco only, so the correct answer was zero results. Same symptom, totally
different cause. The button now greys out and explains the coverage limit.

### Flask was serving a cached page
A frontend change appeared to have no effect. Flask compiles templates once at
startup unless told otherwise, so edits were invisible until a restart. Worth
knowing because it makes you doubt a fix that was actually fine.

### Mapbox will geocode literally anything
Calendar import needs to turn a text location into coordinates. Unconstrained,
`"zzzqqq not a real place 99999"` resolved to **a village in Poland**. Worse,
`"Chase Center"` resolved to **"Chase Court, Fremont"** — a plausible-looking
wrong answer, because Mapbox's older geocoder has thin coverage of venue names.

**Why it was dangerous:** the map's search box has the same weakness, but a human
sees five suggestions and picks the right one. An unattended nightly import takes
the top hit with nobody watching. So the importer now refuses any match below a
confidence threshold and reports it as skipped. Practical consequence: calendar
events need street addresses, not just venue names.

**A second-order bug from the same area:** coordinates were being cached between
syncs, which meant a bad match was *permanent* — fixing the geocoder didn't repair
existing rows. Now every sync re-resolves.

### Postgres quietly rounded the coordinates
Written as `-122.3937`, read back as `-122.394`. The column type was `REAL`,
which in Postgres is a 4-byte float with about six significant digits. In SQLite,
`REAL` is 8 bytes — so this bug **could not appear in local testing**, only after
attaching a real database. About 30 m of error feeding 500 m thresholds.

### Four database connections per page
Opening a connection costs nothing against a local file and a lot against a
database in another state: measured at 654 ms to connect versus 81 ms per query
afterwards. One request was opening four. Now it opens one and reuses it.

### The 511 token was silently rejected in production
The variable was set, the health check confirmed it, and the live site kept
serving invented events. A bad token looked identical to a working one.

**The fix was diagnostic, not functional:** the disruptions endpoint now reports
whether data is live or mocked and, when mocked, why — `401 Unauthorized`, in
plain text, with the key redacted. The same class of invisible failure as the
DataSF outage, which had gone unnoticed for weeks.

**The actual cause, once it was visible:** a single missing digit in the value
pasted into the host's dashboard. Worth sitting with — the bug was trivial, but
finding it was impossible until the system was made to say what was wrong. The
diagnostic took longer to write than the fix, and that was the right trade.

**The pattern across all of these:** every one was a *silent* failure. Nothing
crashed. The app kept looking correct while serving wrong or fake data. Most of
the real work was making failures announce themselves.

---

## 6. Decisions and trade-offs

Being able to explain *why not the other way* is the mark of understanding.

### Calendars over secret links, not "Sign in with Google"
Google Calendar cannot be read with an API key — personal data requires OAuth,
which means a consent screen, a client secret, per-user tokens, and Google's
verification review. It also drags in user accounts and a database.

Google, Apple, and Outlook all publish a **secret calendar link** instead. One
parser handles all three.

**The trade-offs, stated honestly:** it is read-only; the link is a password in
URL form, so it is kept on the server and never sent back to the browser; and
Google caches its output, so an edit can take hours to appear. OAuth fixes all
three and is the right answer *later*, when that lag actually matters.

### One shared passcode, not user accounts
There is one user. A login system would be more code to attack than the thing it
protects. The passcode travels in a custom header, which browsers cannot send
across origins without permission — so there is no cross-site request forgery
risk and no session to manage. Ten wrong guesses from one address starts a
15-minute cooldown, because a short shared secret is otherwise guessable.

**The deliberate asymmetry:** with no passcode set, the gate is *open*. That
keeps local development frictionless, which is exactly why setting it in
production is mandatory rather than optional.

### A database only when something forced it
The project ran a long time with no database on purpose. The trigger was
specific: alerts must work while the app is closed, so a background job has to
read the schedule without a browser. Rows carry a user ID from day one — unused
today, but it turns multi-user into a small migration instead of a rewrite.

### Postgres in production, SQLite locally
One interface, two backends. The whole feature runs locally with no signup. The
cost of that convenience is real and was paid twice: both the `REAL` precision
bug and the connection-per-call problem existed only against Postgres.

### Not using the AI brief yet
The trip brief can use Claude to reason over the assembled facts. That needs a
paid API key, which was deliberately deferred on cost. Without it, a rule-based
version produces the same shape of answer and the interface honestly labels
itself "predicted" rather than "AI brief." Nothing on the live site misrepresents
itself.

---

## 7. Where things stand

**Working in production:** the map, routing, real parking, real civic events,
real highway disruptions, live weather and news in the brief, and a private,
persistent scheduler.

**Check readiness any time** — every `false` is something unconfigured:

```
curl -s https://bay-area-predictive-gps.onrender.com/api/health
```

```
curl -s https://bay-area-predictive-gps.onrender.com/api/disruptions | head -c 200
```

`"source":"511.org"` means real data; `"source":"mock"` comes with a note saying
why.

**Known open items:**

- No email provider yet, so alerts compute but do not send
- The nightly job is scheduled through GitHub Actions, which is free but
  imprecise, and auto-disables after 60 days without repository activity
- Parking and civic events are San Francisco only; every Peninsula and East Bay
  destination comes back empty
- Events, traffic baseline, parking availability, and break-in risk are still
  invented
- Supabase's free tier pauses a project after 7 days of low activity
- Imported calendar events only refresh when you connect or press "Sync now";
  nothing re-syncs them overnight
- "Connect Google Calendar" still means pasting the secret iCal link, not a
  one-click sign-in (that needs OAuth — see §6)

---

## 8. Vocabulary

Terms used throughout, in plain language.

**API key** — a password that identifies your app to someone else's service.
Fine for public data; cannot authorize access to a *person's* private data.

**OAuth** — the "Sign in with Google" flow. A user grants your app permission,
and your app receives a per-user token. Required for personal data.

**Endpoint** — one address on your server that does one job, e.g.
`/api/schedule`.

**Environment variable** — a setting given to a program when it starts, kept
outside the code so secrets are never committed.

**Cron / scheduled job** — something that runs on a timetable rather than when a
person clicks.

**Connection pooler** — a middleman in front of a database that manages
connections. Supabase's is required here because the direct connection needs
IPv6, which the host does not provide.

**Rate limit** — a cap on how many requests a service accepts per hour. 511's is
60; the whole design of that integration follows from it.

**Graceful degradation** — when a part fails, that feature stops and the rest
keeps working. The organizing principle of this project.

**Mock data** — invented data standing in for a real source, with the same shape,
so it can be swapped later with no other changes.

**Geocoding** — turning text into coordinates. The reverse is reverse geocoding.

---

## 9. Questions someone might ask

**"What does it actually do that Maps doesn't?"**
Maps tells you about traffic that already exists. This looks at what is
*scheduled* — construction, events, weather, permitted protests — against trips
it knows you are taking, and warns you the evening before.

**"How much of the data is real?"**
Seven sources are live: routing, weather, news, metered parking, garages, 311
civic events, and 511 highway incidents. Four are still invented: events, traffic
baseline, parking availability, and break-in risk. Each has a documented path to
a real source.

**"Why isn't the AI part turned on?"**
It is built and the switch is a single environment variable. The API is
pay-as-you-go and the cost was not justified yet. The fallback produces the same
shape of answer, and the UI labels itself honestly.

**"What was the hardest bug?"**
The interesting ones were all invisible rather than hard. Parking was broken for
weeks because a data provider changed domains and the frontend never checked for
an error. Postgres silently rounded coordinates in a way that could not reproduce
locally. A 511 token was rejected in production while a health check reported it
as present. The real work was making failures visible.

**"How would this work outside the Bay Area?"**
Routing, weather, and news are already global. 511 is an implementation of the
Open511 standard, so dozens of other agencies work with a configuration change.
The genuinely region-locked parts are parking and civic events, which are
city-by-city — and those are also where the remaining invented data lives.

**"What would you do next?"**
Replace the invented event data with Ticketmaster, use real SFPD data for
break-in risk, and expand parking beyond San Francisco. Longer term, user
accounts and OAuth calendars, which is also what makes the anonymized movement
data layer possible.

---

## Keeping this current

This file should be updated whenever something meaningful changes — a new
feature, a source going from invented to real, a problem diagnosed, or an item in
§7 closing. Ask Claude to update it in the same session the work happens, while
the reasoning is still fresh. The point is not a changelog; it is being able to
explain the system to another person.
