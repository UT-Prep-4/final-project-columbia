import json
import heapq
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

WIKI_API = "https://en.wikipedia.org/w/api.php"
SERPAPI_URL = "https://serpapi.com/search.json"
USER_AGENT = "Lelantos/4.0 (student project; jindolipundit@gmail.com)"
SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY", "d5b4d153cafa1b74f112f7afde1d705741a8f834b7182771e54daa395b817f51")

MAX_MINUTES = 7
ROUND_WIDTH = 10
LINK_CAP = 1600
CLASSIFY_CAP = 500
LINKS_PER_PERSON = 300
ALTERNATE_PATHS = 5
MAX_REROUTES = 3
AI_BRIDGES = 10
AI_ASSOCIATES = 15
NODE_DISPLAY_CAP = 1000
WORKERS = 8
PORT = 8000
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lelantos_cache.json")

BAD_TOKENS = ["list of", "(film)", "(album)", "(song)", "(band)", "(tv series)", "(disambiguation)",
              "(magazine)", "(newspaper)", "(company)", "(video game)", "(novel)", "(play)",
              "championship", "olympics", "world cup", "league", "f.c.", "university of",
              "national team", "awards", "discography", "filmography", "timeline of", "history of"]

NAME_PATTERN = re.compile(r"\b[A-Z][a-z'’\-]+(?:\s+(?:de|van|von|der|bin|al|da|di|la|le))?(?:\s+[A-Z][a-z'’\-]+){1,2}\b")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

isPersonCache = {}
neighborCache = {}
evidenceCache = {}
associatesCache = {}
newsCache = {}
metCache = {}
metMisses = set()

GRAPH = {}
WEIGHTS = {}
STATS = {}
VIRTUAL = set()
virtualLinks = {}

feed = queue.Queue()
searchLock = threading.Lock()
cacheLock = threading.Lock()
statsLock = threading.Lock()


def loadCache():
    global isPersonCache, neighborCache, evidenceCache, associatesCache, newsCache, metCache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            isPersonCache = data.get("isPerson", {})
            neighborCache = data.get("neighbors", {})
            evidenceCache = data.get("evidence", {})
            associatesCache = data.get("associates", {})
            newsCache = data.get("news", {})
            metCache = data.get("met", {})
            print("Cache loaded: " + str(len(neighborCache)) + " people mapped, "
                  + str(len(isPersonCache)) + " titles classified, "
                  + str(len(evidenceCache)) + " connections explained")
        except Exception as error:
            print("Cache load failed: " + str(error))


def saveCache():
    try:
        with cacheLock:
            payload = {"isPerson": dict(isPersonCache), "neighbors": dict(neighborCache),
                       "evidence": dict(evidenceCache), "associates": dict(associatesCache),
                       "news": dict(newsCache), "met": dict(metCache)}
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception as error:
        print("Cache save failed: " + str(error))


def resetStats():
    with statsLock:
        STATS.clear()
        STATS["opened"] = []
        STATS["linksScanned"] = 0
        STATS["screenedOut"] = 0
        STATS["classified"] = 0
        STATS["people"] = 0
        STATS["notPeople"] = 0
        STATS["wikiCalls"] = 0
        STATS["serpCalls"] = 0
        STATS["aiCalls"] = 0
        STATS["metChecks"] = 0
        STATS["failedCalls"] = 0


def bump(field, amount=1):
    with statsLock:
        STATS[field] = STATS.get(field, 0) + amount


def say(text):
    print(text)
    feed.put(text)


# ---------------------------------------------------------------- wikipedia

def wikiQuery(params):
    merged = dict(params)
    merged["action"] = "query"
    merged["format"] = "json"
    merged["formatversion"] = 2
    merged["redirects"] = 1
    for attempt in range(3):
        try:
            bump("wikiCalls")
            response = session.get(WIKI_API, params=merged, timeout=25)
            response.raise_for_status()
            return response.json()
        except Exception:
            if attempt == 2:
                bump("failedCalls")
                return None
    return None


def aliasMap(payload):
    mapping = {}
    for entry in payload.get("query", {}).get("redirects", []):
        mapping[entry.get("from")] = entry.get("to")
    for entry in payload.get("query", {}).get("normalized", []):
        mapping[entry.get("from")] = entry.get("to")
    return mapping


def looksLikeTitleOfPerson(title):
    lowered = title.lower()
    if ":" in title:
        return False
    for character in title:
        if character.isdigit():
            return False
    for token in BAD_TOKENS:
        if token in lowered:
            return False
    if len(title.split()) > 6:
        return False
    return True


def classifyChunk(chunk):
    found = {}
    birthYears = {}
    deathYears = {}
    aliases = {}
    complete = True
    params = {"prop": "categories", "cllimit": "max", "clshow": "!hidden", "titles": "|".join(chunk)}
    while True:
        payload = wikiQuery(params)
        if payload is None or "error" in payload or "query" not in payload:
            # a bad or throttled response must not brand the whole chunk "not a person"
            complete = False
            break
        aliases.update(aliasMap(payload))
        for page in payload.get("query", {}).get("pages", []):
            pageTitle = page.get("title")
            if pageTitle is None:
                continue
            if pageTitle not in found:
                found[pageTitle] = False
            for category in page.get("categories", []):
                catTitle = category.get("title", "")
                lowered = catTitle.lower()
                if " births" in lowered:
                    found[pageTitle] = True
                    match = re.search(r"(\d{3,4}) births", catTitle)
                    if match is not None:
                        birthYears[pageTitle] = int(match.group(1))
                elif " deaths" in lowered:
                    match = re.search(r"(\d{3,4}) deaths", catTitle)
                    if match is not None:
                        deathYears[pageTitle] = int(match.group(1))
        cont = payload.get("continue")
        if not cont:
            break
        params.update(cont)

    if not complete:
        return {}

    verdicts = {}
    for title in chunk:
        canonical = aliases.get(title, title)
        if found.get(canonical, False):
            # person: keep [birth, death] so lifespans can be compared later
            value = [birthYears.get(canonical), deathYears.get(canonical)]
        else:
            value = False
        verdicts[title] = value
        verdicts[canonical] = value

    with cacheLock:
        isPersonCache.update(verdicts)
    bump("classified", len(chunk))
    return verdicts


def classifyTitles(titles):
    unique = []
    for title in titles:
        if title not in unique:
            unique.append(title)

    with cacheLock:
        pending = [title for title in unique if title not in isPersonCache]

    for start in range(0, len(pending), 50):
        classifyChunk(pending[start:start + 50])

    with cacheLock:
        return {title: isPersonCache.get(title, False) for title in unique}


def linkedTitles(title):
    links = []
    params = {"prop": "links", "plnamespace": 0, "pllimit": "max", "titles": title}
    while len(links) < LINK_CAP:
        payload = wikiQuery(params)
        if payload is None:
            break
        for page in payload.get("query", {}).get("pages", []):
            for link in page.get("links", []):
                name = link.get("title")
                if name is not None:
                    links.append(name)
        cont = payload.get("continue")
        if not cont:
            break
        params.update(cont)
    return links[:LINK_CAP]


def personLinks(title):
    if title in VIRTUAL:
        return virtualLinks.get(title, [])

    with cacheLock:
        if title in neighborCache:
            return neighborCache[title]

    links = linkedTitles(title)
    bump("linksScanned", len(links))

    candidates = []
    for name in links:
        if looksLikeTitleOfPerson(name):
            if name not in candidates:
                candidates.append(name)
        else:
            bump("screenedOut")

    candidates = candidates[:CLASSIFY_CAP]
    verdicts = classifyTitles(candidates)

    people = []
    for name in candidates:
        if verdicts.get(name, False):
            people.append(name)
            if len(people) >= LINKS_PER_PERSON:
                break

    bump("people", len(people))
    bump("notPeople", len(candidates) - len(people))

    with cacheLock:
        neighborCache[title] = people
    return people


def wikiSearchTitle(text):
    payload = wikiQuery({"list": "search", "srsearch": text, "srlimit": 1, "srnamespace": 0})
    if payload is None:
        return None
    hits = payload.get("query", {}).get("search", [])
    if len(hits) > 0:
        return hits[0].get("title")
    return None


def confirmPerson(title, strict=False):
    if title is None:
        return None
    if classifyTitles([title]).get(title, False):
        return title
    if strict:
        # a direct search hit cached as "not a person" may be a poisoned entry — recheck once
        with cacheLock:
            isPersonCache.pop(title, None)
        if classifyChunk([title]).get(title, False):
            return title
    return None


def wikiMentions(page, other):
    payload = wikiQuery({"prop": "extracts", "explaintext": 1, "exlimit": 1, "titles": page})
    if payload is None:
        return []
    pages = payload.get("query", {}).get("pages", [])
    if len(pages) == 0:
        return []
    text = pages[0].get("extract", "") or ""
    needles = [other]
    surname = other.split()[-1]
    if len(surname) > 3 and surname != other:
        needles.append(surname)
    hits = []
    for raw in text.replace("\n", " ").split(". "):
        for needle in needles:
            if needle in raw:
                sentence = raw.strip()
                if len(sentence) > 280:
                    sentence = sentence[:277] + "..."
                hits.append({"page": page, "sentence": sentence})
                break
        if len(hits) >= 2:
            break
    return hits


# ---------------------------------------------------------------- serpapi

def serpGet(params):
    if SERPAPI_KEY == "":
        return None
    merged = dict(params)
    merged["api_key"] = SERPAPI_KEY
    try:
        bump("serpCalls")
        response = session.get(SERPAPI_URL, params=merged, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as error:
        bump("failedCalls")
        print("SerpApi call failed: " + str(error))
        return None


def kgLookup(name, context):
    query = name
    if context is not None and context.strip() != "":
        query = name + " " + context.strip()
    data = serpGet({"engine": "google", "q": query + " wikipedia",
                    "hl": "en", "gl": "us", "google_domain": "google.com"})
    if data is None:
        return None
    graph = data.get("knowledge_graph") or {}
    title = graph.get("title")
    if title is not None:
        return title
    for entry in data.get("organic_results", []):
        link = entry.get("link", "")
        if "en.wikipedia.org/wiki/" in link:
            return link.split("/wiki/")[-1].replace("_", " ")
    return None


def aiModeSearch(query):
    data = serpGet({"engine": "google_ai_mode", "q": query, "hl": "en", "gl": "us"})
    if data is None or "error" in data:
        return "", []
    bump("aiCalls")

    parts = []

    def walk(blocks):
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            snippet = block.get("snippet")
            if snippet:
                parts.append(snippet)
            walk(block.get("text_blocks"))
            items = block.get("list")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        s = item.get("snippet")
                        if s:
                            parts.append(s)
                        walk(item.get("text_blocks"))

    walk(data.get("text_blocks"))
    if len(parts) == 0:
        # response shape varies — fall back to any flat answer fields
        for field in ("answer", "snippet", "markdown"):
            value = data.get(field)
            if isinstance(value, str) and value.strip() != "":
                parts.append(value.strip())
                break
    text = "\n".join(parts)

    sources = []
    seen = set()
    for ref in data.get("references", []) or []:
        if not isinstance(ref, dict):
            continue
        link = ref.get("link")
        if link is None or link in seen:
            continue
        seen.add(link)
        title = (ref.get("title") or link).replace("\\u0026", "&").replace("&amp;", "&")
        sources.append({"title": title, "link": link})
    return text, sources[:8]


def extractNames(text, exclude):
    lowerExclude = set()
    for item in exclude:
        lowerExclude.add(item.strip().lower())
    names = []
    for match in NAME_PATTERN.findall(text or ""):
        name = match.strip()
        if name.lower() in lowerExclude:
            continue
        if name not in names:
            names.append(name)
    return names


def confirmedPeopleFrom(text, exclude, cap):
    candidates = extractNames(text, exclude)[:25]
    if len(candidates) == 0:
        return []
    verdicts = classifyTitles(candidates)
    people = []
    for name in candidates:
        if verdicts.get(name, False) and name not in people:
            people.append(name)
            if len(people) >= cap:
                break
    return people


def bioLookup(name, context):
    """Search '<name> biography' and pull out just a clean, usable name."""
    query = name
    if context is not None and context.strip() != "":
        query += " " + context.strip()
    data = serpGet({"engine": "google", "q": query + " biography",
                    "hl": "en", "gl": "us", "google_domain": "google.com"})
    if data is None:
        return None
    graph = data.get("knowledge_graph") or {}
    title = graph.get("title")
    if title is not None and title.strip() != "":
        return title.strip()
    for entry in data.get("organic_results", [])[:6]:
        link = entry.get("link", "")
        if "en.wikipedia.org/wiki/" in link:
            return link.split("/wiki/")[-1].replace("_", " ")
        heading = entry.get("title") or ""
        piece = re.split(r"\s[-–—|·:(]\s?", heading)[0].strip()
        match = NAME_PATTERN.match(piece)
        if match is not None and match.group(0) == piece and len(piece.split()) <= 4:
            return piece
    return None


def aiAssociates(name, context):
    key = (name + "||" + (context or "")).lower()
    with cacheLock:
        if key in associatesCache:
            return associatesCache[key]
    query = ("Who are the most notable people closely connected to " + name)
    if context is not None and context.strip() != "":
        query += " (" + context.strip() + ")"
    query += "? Collaborators, colleagues, mentors, friends or family who have Wikipedia pages. List their full names."
    say("searching deeper for who " + name + " is connected to")
    text, _ = aiModeSearch(query)
    people = confirmedPeopleFrom(text, {name}, AI_ASSOCIATES)
    with cacheLock:
        associatesCache[key] = people
    return people


def aiBridges(a, b):
    query = ("Which well-known people directly connect " + a + " and " + b +
             "? Think of mutual collaborators, co-stars, friends, or colleagues both of them knew or worked with. List their full names.")
    say("searching deeper for people who bridge " + a + " and " + b)
    text, _ = aiModeSearch(query)
    return confirmedPeopleFrom(text, {a, b}, AI_BRIDGES)


def aiMetPeople(name):
    key = name.lower() + "||met"
    with cacheLock:
        if key in associatesCache:
            return associatesCache[key]
    say("searching deeper for people " + name + " actually met")
    text, _ = aiModeSearch("Which famous or notable people did " + name +
                           " actually meet in person during their lifetime? List their full names.")
    people = confirmedPeopleFrom(text, {name}, AI_ASSOCIATES)
    with cacheLock:
        associatesCache[key] = people
    return people


def newsCoverage(a, b):
    key = makeKey(a, b)
    with cacheLock:
        if key in newsCache:
            entry = newsCache[key]
            return entry["hit"], entry["stories"]

    hit = False
    stories = []
    data = serpGet({"engine": "google_news", "q": '"' + a + '" "' + b + '"', "hl": "en", "gl": "us"})
    if data is not None:
        flat = []
        for entry in data.get("news_results", []) or []:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("stories"), list):
                for sub in entry["stories"]:
                    if isinstance(sub, dict):
                        flat.append(sub)
            else:
                flat.append(entry)
        surnameA = a.split()[-1].lower()
        surnameB = b.split()[-1].lower()
        matched = 0
        for item in flat[:40]:
            title = item.get("title") or ""
            link = item.get("link")
            if link is None:
                continue
            lowered = title.lower()
            if surnameA in lowered and surnameB in lowered:
                matched += 1
                if len(stories) < 5:
                    source = item.get("source")
                    if isinstance(source, dict):
                        source = source.get("name")
                    stories.append({"title": title, "link": link,
                                    "date": item.get("date") or "",
                                    "source": source or ""})
        hit = matched >= 2

    with cacheLock:
        newsCache[key] = {"hit": hit, "stories": stories}
    return hit, stories


def lifespanOf(title):
    with cacheLock:
        value = isPersonCache.get(title)
    if isinstance(value, list) and len(value) == 2:
        return value[0], value[1]
    return None, None


def couldHaveMet(a, b):
    birthA, deathA = lifespanOf(a)
    birthB, deathB = lifespanOf(b)
    if birthA is None or birthB is None:
        return True
    endA = deathA if deathA is not None else 9999
    endB = deathB if deathB is not None else 9999
    # each must have been at least ~7 years old while the other was alive
    return (birthA + 7) <= endB and (birthB + 7) <= endA


def plausibleMatch(typed, title):
    """A search hit only counts if it plausibly matches the typed name."""
    titleWords = [word.lower().strip(".,()") for word in title.split()]
    for token in typed.lower().split():
        if len(token) < 3:
            continue
        prefix = token[:4]
        hit = False
        for word in titleWords:
            if word == token or word.startswith(prefix) or (len(word) >= 4 and token.startswith(word[:4])):
                hit = True
                break
        if not hit:
            return False
    return True


def verifyMet(a, b):
    key = makeKey(a, b)
    with cacheLock:
        if key in metCache:
            return metCache[key]

    if SERPAPI_KEY == "" or key in metMisses:
        return {"met": None, "ai": "", "sources": []}

    say("verifying that " + a + " and " + b + " really met")
    bump("metChecks")
    query = ("Did " + a + " and " + b + " ever meet in person, or know each other personally, "
             "well enough that they would recognize each other and could call one another? "
             "Start your answer with the single word YES or NO, then explain how they know "
             "each other, when and where they met, with specific facts.")
    text, sources = aiModeSearch(query)

    met = None
    head = text.strip().lower()[:200]
    if head.startswith("yes"):
        met = True
    elif head.startswith("no"):
        met = False
    elif "never met" in head or "did not meet" in head or "no evidence" in head or "no record" in head:
        met = False
    elif head.startswith("**yes") or head[:40].find("yes,") >= 0 or head[:40].find("yes.") >= 0:
        met = True

    verdict = {"met": met, "ai": text.strip(), "sources": sources}
    if text.strip() != "":
        with cacheLock:
            metCache[key] = verdict
    else:
        # empty answer — don't ask again this session
        metMisses.add(key)
    return verdict


def resolvePerson(name, context):
    attempts = [name]
    if context is not None and context.strip() != "":
        attempts.insert(0, name + " " + context.strip())

    for text in attempts:
        hit = wikiSearchTitle(text)
        if hit is not None and not plausibleMatch(name, hit):
            continue
        person = confirmPerson(hit, strict=True)
        if person is not None:
            return person

    if SERPAPI_KEY == "":
        return None

    say("no direct match — searching deeper for " + name)

    corrected = kgLookup(name, context)
    if corrected is not None:
        person = confirmPerson(corrected, strict=True)
        if person is None:
            hit = wikiSearchTitle(corrected)
            if hit is not None and plausibleMatch(corrected, hit):
                person = confirmPerson(hit, strict=True)
        if person is not None:
            if person.lower() != name.strip().lower():
                say("reading " + name + " as " + person)
            return person

    guess = bioLookup(name, context)
    if guess is not None and guess.lower() != name.strip().lower():
        person = confirmPerson(guess, strict=True)
        if person is None:
            hit = wikiSearchTitle(guess)
            if hit is not None and plausibleMatch(guess, hit):
                person = confirmPerson(hit, strict=True)
        if person is not None:
            say("reading " + name + " as " + person)
            return person

    return None


def ensurePerson(name, context):
    say("looking up " + name)
    title = resolvePerson(name, context)
    if title is not None:
        return title

    if context is None or context.strip() == "":
        return None

    say("no biography surfaced for " + name + " — searching deeper with your context")
    associates = aiAssociates(name, context)
    if len(associates) == 0:
        return None

    title = " ".join(part.capitalize() if part.islower() else part for part in name.strip().split())
    VIRTUAL.add(title)
    virtualLinks[title] = associates
    say(name + " joins the web through " + str(len(associates)) + " known associates")
    return title


# ---------------------------------------------------------------- graph

def makeKey(a, b):
    pair = sorted([a, b])
    return pair[0] + "|||" + pair[1]


def addEdge(a, b, weight):
    if a not in GRAPH:
        GRAPH[a] = set()
    if b not in GRAPH:
        GRAPH[b] = set()
    GRAPH[a].add(b)
    GRAPH[b].add(a)
    key = makeKey(a, b)
    WEIGHTS[key] = max(WEIGHTS.get(key, 0), weight)


def edgeWeight(a, b):
    return WEIGHTS.get(makeKey(a, b), 1)


def edgeCost(a, b):
    weight = edgeWeight(a, b)
    if weight >= 3:
        return 0.7
    if weight >= 2:
        return 0.85
    return 1.0


def expandPerson(title):
    say("opening " + title)
    with statsLock:
        STATS["opened"].append(title)
    return title, personLinks(title)


def expandLevel(names):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for title, links in pool.map(expandPerson, names):
            for other in links:
                if not couldHaveMet(title, other):
                    continue
                with cacheLock:
                    back = neighborCache.get(other, [])
                addEdge(title, other, 2 if title in back else 1)


def crawlBetween(startName, targetName, contextA, contextB):
    GRAPH.clear()
    WEIGHTS.clear()

    cleanStart = ensurePerson(startName, contextA)
    if cleanStart is None:
        return None, None

    cleanTarget = ensurePerson(targetName, contextB)
    if cleanTarget is None:
        return cleanStart, None

    if cleanStart == cleanTarget:
        return cleanStart, cleanTarget

    if SERPAPI_KEY != "" and cleanStart not in VIRTUAL and cleanTarget not in VIRTUAL:
        say("checking the news for " + cleanStart + " and " + cleanTarget)
        hit, _ = newsCoverage(cleanStart, cleanTarget)
        if hit and couldHaveMet(cleanStart, cleanTarget):
            say("they appear in the news together")
            addEdge(cleanStart, cleanTarget, 3)

    expandLevel([cleanStart, cleanTarget])

    frontierA = [cleanStart]
    frontierB = [cleanTarget]
    seenA = set([cleanStart])
    seenB = set([cleanTarget])
    deadline = time.time() + MAX_MINUTES * 60

    if len(dijkstra(cleanStart, cleanTarget, set(), set())) == 0 and SERPAPI_KEY != "":
        bridges = aiBridges(cleanStart, cleanTarget)
        fresh = [name for name in bridges
                 if name not in seenA and name not in seenB
                 and name != cleanStart and name != cleanTarget]
        if len(fresh) > 0:
            say("deep search suggests: " + ", ".join(fresh))
            expandLevel(fresh)
            for name in fresh:
                seenA.add(name)
                frontierA.append(name)

    rounds = 0
    while time.time() < deadline:
        if len(dijkstra(cleanStart, cleanTarget, set(), set())) > 0:
            say("chain found")
            break

        if len(frontierA) == 0 and len(frontierB) == 0:
            break

        if len(frontierA) > 0 and (len(frontierA) <= len(frontierB) or len(frontierB) == 0):
            source, seen, other = frontierA, seenA, seenB
        else:
            source, seen, other = frontierB, seenB, seenA

        wave = []
        while len(source) > 0 and len(wave) < ROUND_WIDTH:
            name = source.pop(0)
            neighbors = sorted(GRAPH.get(name, set()),
                               key=lambda o: (0 if o in other else 1,
                                              -edgeWeight(name, o),
                                              -len(GRAPH.get(o, ())), o))
            for neighbor in neighbors:
                if neighbor not in seen:
                    seen.add(neighbor)
                    wave.append(neighbor)
                    if len(wave) >= ROUND_WIDTH:
                        break

        if len(wave) == 0:
            break

        expandLevel(wave)
        for name in wave:
            source.append(name)

        rounds += 1
        if rounds % 5 == 0:
            with statsLock:
                openedCount = len(STATS["opened"])
            say("still searching — " + str(openedCount) + " people opened")

    return cleanStart, cleanTarget


def dijkstra(startPerson, targetPerson, bannedEdges, bannedNodes):
    if startPerson not in GRAPH or targetPerson not in GRAPH:
        return []
    if startPerson == targetPerson:
        return [startPerson]

    heap = [(0.0, startPerson, [startPerson])]
    bestCost = {startPerson: 0.0}

    while len(heap) > 0:
        cost, person, path = heapq.heappop(heap)
        if person == targetPerson:
            return path
        if cost > bestCost.get(person, float("inf")):
            continue
        for neighbor in GRAPH[person]:
            if neighbor in bannedNodes:
                continue
            if makeKey(person, neighbor) in bannedEdges:
                continue
            newCost = cost + edgeCost(person, neighbor)
            if newCost < bestCost.get(neighbor, float("inf")):
                bestCost[neighbor] = newCost
                heapq.heappush(heap, (newCost, neighbor, path + [neighbor]))

    return []


def pathCost(path):
    total = 0.0
    for i in range(len(path) - 1):
        total += edgeCost(path[i], path[i + 1])
    return total


def findPaths(startPerson, targetPerson):
    first = dijkstra(startPerson, targetPerson, set(), set())
    if len(first) == 0:
        return []

    accepted = [first]
    candidates = []

    while len(accepted) < ALTERNATE_PATHS:
        previous = accepted[-1]
        for i in range(len(previous) - 1):
            spurNode = previous[i]
            rootPath = previous[:i + 1]

            bannedEdges = set()
            for path in accepted:
                if len(path) > i + 1 and path[:i + 1] == rootPath:
                    bannedEdges.add(makeKey(path[i], path[i + 1]))
            bannedNodes = set(rootPath[:-1])

            spur = dijkstra(spurNode, targetPerson, bannedEdges, bannedNodes)
            if len(spur) == 0:
                continue
            total = rootPath[:-1] + spur
            if total not in accepted and total not in candidates:
                candidates.append(total)

        if len(candidates) == 0:
            break
        candidates.sort(key=lambda path: (len(path), pathCost(path)))
        accepted.append(candidates.pop(0))

    accepted.sort(key=lambda path: (len(path), pathCost(path)))
    return accepted


def buildEdges(path):
    edges = []
    for i in range(len(path) - 1):
        a = path[i]
        b = path[i + 1]
        with cacheLock:
            verdict = metCache.get(makeKey(a, b))
        met = verdict["met"] if verdict is not None else None
        edges.append({"from": a, "to": b, "weight": edgeWeight(a, b), "met": met})
    return edges


def describePath(path):
    edges = buildEdges(path)
    strength = 0
    for edge in edges:
        strength += edge["weight"]
    return {"path": path, "edges": edges, "degrees": len(path) - 1, "strength": strength}


def graphPayload(pathList):
    onPath = set()
    for path in pathList:
        onPath.update(path)
    with statsLock:
        opened = set(STATS.get("opened", []))

    include = []
    for title in onPath:
        include.append(title)
    for title in opened:
        if title not in onPath:
            include.append(title)

    others = [title for title in GRAPH if title not in onPath and title not in opened]
    others.sort(key=lambda title: -len(GRAPH.get(title, ())))
    for title in others:
        if len(include) >= NODE_DISPLAY_CAP:
            break
        include.append(title)

    included = set(include)
    nodes = []
    for title in include:
        nodes.append({"id": title,
                      "opened": title in opened,
                      "virtual": title in VIRTUAL})

    links = []
    for key, weight in WEIGHTS.items():
        a, _, b = key.partition("|||")
        if a in included and b in included:
            links.append({"source": a, "target": b, "weight": weight})

    return {"nodes": nodes, "links": links}


# ---------------------------------------------------------------- evidence

def evidenceBetween(a, b):
    key = makeKey(a, b)
    with cacheLock:
        if key in evidenceCache:
            return evidenceCache[key]

    result = {"a": a, "b": b, "ai": "", "sources": [], "wiki": [], "news": [], "met": None,
              "virtual": (a in VIRTUAL or b in VIRTUAL)}

    if a not in VIRTUAL and b not in VIRTUAL:
        result["wiki"] = wikiMentions(a, b) + wikiMentions(b, a)
        if SERPAPI_KEY != "":
            _, stories = newsCoverage(a, b)
            result["news"] = stories

    verdict = verifyMet(a, b)
    result["ai"] = verdict["ai"]
    result["sources"] = verdict["sources"]
    result["met"] = verdict["met"]

    with cacheLock:
        evidenceCache[key] = result
    return result


# ---------------------------------------------------------------- search

def snapshot():
    with statsLock:
        data = dict(STATS)
        data["opened"] = list(STATS.get("opened", []))
    data["nodes"] = len(GRAPH)
    data["edges"] = len(WEIGHTS)
    return data


def search(nameA, nameB, contextA, contextB):
    resetStats()
    cleanA, cleanB = crawlBetween(nameA, nameB, contextA, contextB)

    if cleanA is None:
        return {"status": "not_found", "missing": nameA, "side": "a", "stats": snapshot(),
                "paths": [], "graph": {"nodes": [], "links": []}}
    if cleanB is None:
        return {"status": "not_found", "missing": nameB, "side": "b", "stats": snapshot(),
                "paths": [], "graph": {"nodes": [], "links": []}}

    if cleanA == cleanB:
        only = describePath([cleanA])
        return {"status": "found", "start": cleanA, "target": cleanB,
                "degrees": 0, "strength": 0, "paths": [only],
                "graph": graphPayload([[cleanA]]), "stats": snapshot()}

    paths = findPaths(cleanA, cleanB)

    reroutes = 0
    augmented = set()
    while len(paths) > 0 and reroutes < MAX_REROUTES and SERPAPI_KEY != "":
        best = paths[0]
        badPair = None
        for i in range(len(best) - 1):
            # verify in order and stop at the first broken hop — no wasted checks
            verdict = verifyMet(best[i], best[i + 1])
            if verdict["met"] is False:
                badPair = (best[i], best[i + 1])
                break
        if badPair is None:
            break
        say(badPair[0] + " and " + badPair[1] + " never actually met — rerouting")
        WEIGHTS.pop(makeKey(badPair[0], badPair[1]), None)
        GRAPH.get(badPair[0], set()).discard(badPair[1])
        GRAPH.get(badPair[1], set()).discard(badPair[0])
        reroutes += 1

        # if an endpoint's own connections are the problem, ask who they REALLY met
        for endpoint in (cleanA, cleanB):
            if endpoint in badPair and endpoint not in augmented and endpoint not in VIRTUAL:
                augmented.add(endpoint)
                really = [person for person in aiMetPeople(endpoint)
                          if person != cleanA and person != cleanB
                          and couldHaveMet(endpoint, person)]
                if len(really) > 0:
                    say(endpoint + " actually met: " + ", ".join(really))
                    for person in really:
                        addEdge(endpoint, person, 2)
                    expandLevel(really)

        paths = findPaths(cleanA, cleanB)

    saveCache()

    if len(paths) == 0:
        return {"status": "no_path", "start": cleanA, "target": cleanB,
                "stats": snapshot(), "paths": [], "graph": graphPayload([])}

    described = [describePath(path) for path in paths]
    best = described[0]

    return {"status": "found", "start": cleanA, "target": cleanB,
            "degrees": best["degrees"], "strength": best["strength"],
            "paths": described, "graph": graphPayload(paths), "stats": snapshot()}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lelantos</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500&family=EB+Garamond:ital,wght@0,400;0,500;1,400&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  :root { --ink:#e6e0d1; --dim:#7d7869; --gold:#c9b57b; --line:#2c2f38; --bg:#0c0e13; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"EB Garamond", Georgia, serif; font-size:18px; line-height:1.6;
    display:flex; align-items:center; justify-content:center; overflow:hidden;
  }
  canvas#web { position:fixed; inset:0; z-index:0; opacity:0; animation:veil 3s ease forwards .4s; }
  @keyframes veil { to { opacity:1; } }

  #graph3d { position:fixed; inset:0; z-index:0; display:none; }
  #graph3d.shown { display:block; }

  main {
    position:relative; z-index:1; width:100%; max-width:640px; padding:40px 28px;
    text-align:center; transition:opacity .8s ease;
  }
  main.hidden { opacity:0; pointer-events:none; }

  h1 {
    font-family:"Cinzel", Georgia, serif; font-weight:400;
    font-size:clamp(46px,10vw,78px); margin:0; color:var(--gold);
    letter-spacing:.22em; text-indent:.22em; opacity:0;
    animation:settle 1.6s cubic-bezier(.2,.7,.2,1) forwards;
  }
  @keyframes settle {
    from { opacity:0; letter-spacing:.9em; text-indent:.9em; filter:blur(10px); }
    to   { opacity:1; letter-spacing:.22em; text-indent:.22em; filter:blur(0); }
  }

  .rule { width:0; height:1px; margin:22px auto 18px;
    background:linear-gradient(90deg, transparent, var(--gold), transparent);
    animation:draw 1.4s ease forwards 1s; }
  @keyframes draw { to { width:220px; } }

  .epigraph { margin:0 auto 40px; max-width:480px; color:var(--dim); font-style:italic;
    opacity:0; animation:rise 1.2s ease forwards 1.5s; }

  .fields { display:flex; gap:18px; align-items:flex-end; opacity:0;
    animation:rise 1.2s ease forwards 1.9s; }
  @keyframes rise { from { opacity:0; transform:translateY(14px); } to { opacity:1; transform:none; } }

  .field { position:relative; flex:1; }
  .tag { font-size:11px; letter-spacing:.28em; text-transform:uppercase; color:#55524a; margin-bottom:4px; }
  input { width:100%; padding:12px 8px 10px; background:transparent; border:none;
    border-bottom:1px solid var(--line); color:var(--ink);
    font-family:inherit; font-size:20px; text-align:center; outline:none; }
  input::placeholder { color:#4a473f; }
  input:focus { border-bottom-color:transparent; }
  .underglow { position:absolute; left:50%; bottom:0; width:0; height:1px; background:var(--gold);
    transform:translateX(-50%); transition:width .6s cubic-bezier(.2,.8,.2,1); }
  input:focus ~ .underglow { width:100%; }
  .arrow { padding-bottom:52px; color:var(--line); font-size:22px; }
  .ctx { margin-top:10px; font-size:14px; font-style:italic; color:var(--dim);
    border-bottom:1px dashed rgba(44,47,56,.9); padding:5px 8px; }
  .ctx::placeholder { color:#3d3b35; font-style:italic; }
  .ctx:focus { border-bottom:1px dashed rgba(201,181,123,.55); }

  .hint { margin-top:22px; min-height:26px; font-size:13px; letter-spacing:.16em;
    text-transform:uppercase; color:var(--dim); }
  .hint b { color:var(--gold); font-weight:500; }

  #rescue {
    margin:20px auto 0; max-width:460px; text-align:left;
    border:1px solid transparent; border-radius:3px;
    background:rgba(18,21,28,.85);
    max-height:0; opacity:0; overflow:hidden; padding:0 20px;
    transition:max-height .6s cubic-bezier(.2,.8,.2,1), opacity .5s ease, padding .6s ease, border-color .6s ease;
  }
  #rescue.shown { max-height:300px; opacity:1; padding:18px 20px 20px; border-color:var(--line); }
  #rescue p { margin:0 0 14px; font-size:15px; color:var(--dim); font-style:italic; }
  #rescue p b { color:var(--gold); font-style:normal; }
  #rescue input { text-align:left; font-size:17px; }
  #rescue .row { display:flex; gap:12px; align-items:center; margin-top:16px; }

  .button {
    display:inline-block; cursor:pointer; user-select:none;
    font-family:"EB Garamond", Georgia, serif; font-size:13px;
    letter-spacing:.22em; text-transform:uppercase;
    color:var(--gold); border:1px solid rgba(201,181,123,.5);
    border-radius:2px; padding:9px 22px; background:rgba(201,181,123,.08);
    transition:background .25s ease, border-color .25s ease, color .25s ease, box-shadow .25s ease;
  }
  .button:hover {
    background:rgba(201,181,123,.18); border-color:var(--gold); color:#f7ecca;
    box-shadow:0 0 20px rgba(201,181,123,.22);
  }
  .ghost { color:var(--dim); border-color:rgba(125,120,105,.3); background:transparent; }
  .ghost:hover { color:var(--gold); background:rgba(201,181,123,.06);
    border-color:rgba(201,181,123,.5); box-shadow:none; }

  #console {
    position:fixed; left:0; right:0; bottom:0; z-index:2; height:130px;
    padding:14px 20px; overflow:hidden; text-align:left;
    font-family:"JetBrains Mono", monospace; font-size:12px; line-height:1.7;
    color:#5f6b57; pointer-events:none;
    -webkit-mask-image:linear-gradient(to top, black 40%, transparent);
    mask-image:linear-gradient(to top, black 40%, transparent);
    opacity:0; transition:opacity .5s ease;
  }
  #console.shown { opacity:1; }
  #console .live { color:var(--gold); }

  #verdict { position:fixed; top:34px; left:0; right:0; z-index:2; text-align:center;
    pointer-events:none; opacity:0; transition:opacity 1s ease .4s; }
  #verdict.shown { opacity:1; }
  #verdict .count { font-family:"Cinzel", Georgia, serif; font-size:42px; color:var(--gold); letter-spacing:.1em; }
  #verdict .of { font-size:12px; letter-spacing:.24em; text-transform:uppercase; color:var(--dim); }

  #hops { position:fixed; left:0; right:0; bottom:118px; z-index:3;
    display:flex; gap:10px; justify-content:center; flex-wrap:wrap; padding:0 20px;
    opacity:0; pointer-events:none; transition:opacity .8s ease .8s; }
  #hops.shown { opacity:1; pointer-events:auto; }
  .hop { cursor:pointer; font-size:14px; padding:7px 14px; border:1px solid var(--line);
    border-radius:2px; background:rgba(12,14,19,.88); color:var(--ink);
    transition:border-color .25s ease, background .25s ease; }
  .hop i { color:var(--dim); font-style:normal; margin:0 6px; }
  .hop small { display:block; font-size:10px; letter-spacing:.22em; text-transform:uppercase;
    color:#55524a; text-align:center; margin-top:1px; }
  .hop:hover { border-color:var(--gold); background:rgba(201,181,123,.08); }

  #controls { position:fixed; top:34px; right:26px; z-index:4;
    display:flex; flex-direction:column; gap:10px; align-items:flex-end;
    opacity:0; pointer-events:none; transition:opacity .8s ease 1s; }
  #controls.shown { opacity:1; pointer-events:auto; }

  .panel {
    position:fixed; top:0; bottom:0; width:400px; z-index:5;
    background:rgba(9,11,15,.97); padding:34px 26px 40px; overflow-y:auto;
    transition:transform .7s cubic-bezier(.2,.8,.2,1);
  }
  #panelWrap { right:0; border-left:1px solid var(--line); transform:translateX(100%); }
  #panelWrap.shown { transform:none; }
  #evidence { left:0; border-right:1px solid var(--line); transform:translateX(-100%); }
  #evidence.shown { transform:none; }

  .panel h2 { font-family:"Cinzel", Georgia, serif; font-weight:400; font-size:15px;
    letter-spacing:.24em; text-transform:uppercase; color:var(--gold); margin:0 0 18px; }
  .panel h3 { font-size:11px; letter-spacing:.26em; text-transform:uppercase;
    color:#55524a; margin:28px 0 10px; font-weight:400; }
  .closer { position:absolute; top:24px; right:24px; cursor:pointer; color:var(--dim); font-size:22px; }
  .closer:hover { color:var(--gold); }

  #eviPair { font-size:20px; margin-bottom:4px; }
  #eviPair i { color:var(--dim); font-style:normal; margin:0 8px; }
  #eviBody p { font-size:15px; color:var(--ink); margin:0 0 12px; }
  #eviBody .quote { font-size:14px; font-style:italic; color:var(--dim);
    border-left:2px solid rgba(201,181,123,.4); padding:2px 0 2px 12px; margin:0 0 12px; }
  #eviBody .quote b { color:var(--gold); font-style:normal; font-weight:500; }
  #eviBody a { color:var(--gold); text-decoration:none; font-size:14px; display:block;
    padding:4px 0; border-bottom:1px dotted rgba(44,47,56,.7); }
  #eviBody a:hover { color:#f7ecca; }
  #eviBody a .when { display:block; color:#55524a; font-size:11px; letter-spacing:.12em; text-transform:uppercase; }
  .metBadge { display:inline-block; font-size:11px; letter-spacing:.22em; text-transform:uppercase;
    padding:6px 12px; border:1px solid; border-radius:2px; margin-bottom:14px !important; }
  .metBadge.yes { color:#8fae7f; border-color:rgba(143,174,127,.45); }
  .metBadge.no { color:#b0713e; border-color:rgba(176,113,62,.45); }
  #eviBody .note { font-size:13px; color:#8a6a4f; font-style:italic; }
  .loadingDots { color:var(--dim); font-style:italic; }

  .stat { display:flex; justify-content:space-between; font-size:15px;
    padding:5px 0; border-bottom:1px dotted rgba(44,47,56,.7); }
  .stat span:last-child { color:var(--gold); font-family:"JetBrains Mono", monospace; font-size:13px; }

  .chain { border:1px solid var(--line); border-radius:2px; padding:11px 13px; margin-bottom:9px;
    cursor:pointer; transition:border-color .25s ease, background .25s ease; }
  .chain:hover { border-color:rgba(201,181,123,.5); background:rgba(201,181,123,.05); }
  .chain.active { border-color:var(--gold); background:rgba(201,181,123,.09); }
  .chain .top { display:flex; justify-content:space-between; font-size:11px;
    letter-spacing:.18em; text-transform:uppercase; color:#55524a; margin-bottom:6px; }
  .chain .top b { color:var(--gold); font-weight:500; }
  .chain .names { font-size:15px; line-height:1.5; color:var(--ink); }
  .chain .names i { color:var(--dim); font-style:normal; }

  #opened { font-family:"JetBrains Mono", monospace; font-size:11px; line-height:1.9;
    color:#5f6b57; max-height:200px; overflow-y:auto; }

  #legend { position:fixed; left:26px; bottom:140px; z-index:2; font-size:11px;
    letter-spacing:.18em; text-transform:uppercase; color:#55524a;
    opacity:0; transition:opacity .8s ease 1.2s; pointer-events:none; }
  #legend.shown { opacity:1; }
  #legend div { margin:3px 0; }
  #legend span { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; vertical-align:middle; }
</style>
</head>
<body>
<canvas id="web"></canvas>
<div id="graph3d"></div>

<div id="verdict">
  <div class="count" id="degrees"></div>
  <div class="of" id="strengthLine">Degrees of separation</div>
</div>

<main id="panel">
  <h1>Lelantos</h1>
  <div class="rule"></div>
  <p class="epigraph">Name two people from history or headlines. We search Wikipedia, the news, and the wider web live and trace the chain that links them.</p>
  <div class="fields">
    <div class="field">
      <div class="tag">From</div>
      <input id="from" type="text" placeholder="Frida Kahlo" autocomplete="off" spellcheck="false" autofocus>
      <div class="underglow"></div>
      <input id="fromCtx" class="ctx" type="text" placeholder="which one? &mdash; optional" autocomplete="off" spellcheck="false">
    </div>
    <div class="arrow">&rarr;</div>
    <div class="field">
      <div class="tag">To</div>
      <input id="to" type="text" placeholder="David Bowie" autocomplete="off" spellcheck="false">
      <div class="underglow"></div>
      <input id="toCtx" class="ctx" type="text" placeholder="which one? &mdash; optional" autocomplete="off" spellcheck="false">
    </div>
  </div>
  <div class="hint" id="hint"></div>

  <div id="rescue">
    <p>No biography matched <b id="missingName"></b>. Tell me who they are — a job, a city, anything —
       and we will search deeper and place them in the web.</p>
    <div class="tag">Context</div>
    <input id="context" type="text" placeholder="sleep researcher at Yale" autocomplete="off">
    <div class="row">
      <div class="button" id="retry">Look again</div>
      <div class="button ghost" id="dismiss">Never mind</div>
    </div>
  </div>
</main>

<div id="controls">
  <div class="button" id="again">Search again</div>
  <div class="button ghost" id="showPanel">Show detail</div>
</div>

<div id="hops"></div>

<div id="legend">
  <div><span style="background:#f3e7c3"></span>Endpoints</div>
  <div><span style="background:#c9b57b"></span>The chain</div>
  <div><span style="background:#6f7f63"></span>Pages opened</div>
  <div><span style="background:#39404f"></span>People seen</div>
  <div><span style="background:#b0713e"></span>Known only by context</div>
</div>

<div id="evidence" class="panel">
  <div class="closer" id="closeEvidence">&times;</div>
  <h2>The connection</h2>
  <div id="eviPair"></div>
  <div id="eviBody"></div>
</div>

<div id="panelWrap" class="panel">
  <div class="closer" id="closePanel">&times;</div>
  <h2>Search detail</h2>
  <div id="counts"></div>
  <h3>Chains found</h3>
  <div id="chains"></div>
  <h3>People opened</h3>
  <div id="opened"></div>
</div>

<div id="console"></div>

<script type="module">
  try {
    const [fg, st] = await Promise.all([
      import('https://esm.sh/3d-force-graph@1.79.0'),
      import('https://esm.sh/three-spritetext@1.10.0')
    ]);
    window.Lib3D = { ForceGraph3D: fg.default, SpriteText: st.default };
  } catch (e) {
    console.error('3D library failed to load', e);
    window.Lib3D = null;
  }
</script>

<script>
  const canvas = document.getElementById("web");
  const ctx = canvas.getContext("2d");
  const panel = document.getElementById("panel");
  const hint = document.getElementById("hint");
  const verdict = document.getElementById("verdict");
  const degrees = document.getElementById("degrees");
  const strengthLine = document.getElementById("strengthLine");
  const controls = document.getElementById("controls");
  const again = document.getElementById("again");
  const showPanel = document.getElementById("showPanel");
  const panelWrap = document.getElementById("panelWrap");
  const closePanel = document.getElementById("closePanel");
  const counts = document.getElementById("counts");
  const chains = document.getElementById("chains");
  const openedBox = document.getElementById("opened");
  const rescue = document.getElementById("rescue");
  const missingName = document.getElementById("missingName");
  const contextBox = document.getElementById("context");
  const retry = document.getElementById("retry");
  const dismiss = document.getElementById("dismiss");
  const fromBox = document.getElementById("from");
  const toBox = document.getElementById("to");
  const fromCtx = document.getElementById("fromCtx");
  const toCtx = document.getElementById("toCtx");
  const cons = document.getElementById("console");
  const graphBox = document.getElementById("graph3d");
  const hops = document.getElementById("hops");
  const legend = document.getElementById("legend");
  const evidence = document.getElementById("evidence");
  const closeEvidence = document.getElementById("closeEvidence");
  const eviPair = document.getElementById("eviPair");
  const eviBody = document.getElementById("eviBody");

  let drifters = [], w = 0, h = 0;
  let mode = "idle";
  let feedTimer = null;
  let missingSide = "";
  let Graph = null;
  let lastData = null;
  let pathNodeSet = new Set();
  let pathEdgeSet = new Set();
  let endpointSet = new Set();
  let virtualSet = new Set();

  function keyOf(a, b) { return a < b ? a + "|||" + b : b + "|||" + a; }
  function idOf(x) { return typeof x === "object" && x !== null ? x.id : x; }

  /* ---------- idle drifting web ---------- */
  function seed() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
    const count = Math.max(28, Math.round((w * h) / 34000));
    drifters = [];
    for (let i = 0; i < count; i++)
      drifters.push({ x:Math.random()*w, y:Math.random()*h,
        vx:(Math.random()-.5)*.16, vy:(Math.random()-.5)*.16, r:Math.random()*1.3+.5 });
    if (Graph) { Graph.width(window.innerWidth); Graph.height(window.innerHeight); }
  }

  function frame() {
    if (mode === "idle") {
      ctx.clearRect(0, 0, w, h);
      for (const n of drifters) {
        n.x += n.vx; n.y += n.vy;
        if (n.x < 0 || n.x > w) n.vx *= -1;
        if (n.y < 0 || n.y > h) n.vy *= -1;
      }
      for (let i = 0; i < drifters.length; i++)
        for (let j = i + 1; j < drifters.length; j++) {
          const dx = drifters[i].x - drifters[j].x, dy = drifters[i].y - drifters[j].y;
          const d = Math.hypot(dx, dy);
          if (d < 155) {
            ctx.strokeStyle = "rgba(201,181,123," + (.16*(1-d/155)) + ")";
            ctx.lineWidth = .6; ctx.beginPath();
            ctx.moveTo(drifters[i].x, drifters[i].y); ctx.lineTo(drifters[j].x, drifters[j].y); ctx.stroke();
          }
        }
      for (const n of drifters) {
        ctx.fillStyle = "rgba(230,224,209,.35)";
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI*2); ctx.fill();
      }
    }
    requestAnimationFrame(frame);
  }

  /* ---------- 3d web ---------- */
  function nodeColor(n) {
    if (endpointSet.has(n.id)) return "#f3e7c3";
    if (pathNodeSet.has(n.id)) return "#c9b57b";
    if (n.virtual) return "#b0713e";
    if (n.opened) return "#6f7f63";
    return "#39404f";
  }

  function linkOnPath(l) { return pathEdgeSet.has(keyOf(idOf(l.source), idOf(l.target))); }

  function nodeObject(n) {
    if (!pathNodeSet.has(n.id) || !window.Lib3D) return undefined;
    const sprite = new window.Lib3D.SpriteText(n.id);
    sprite.color = endpointSet.has(n.id) ? "#f7ecca" : "#e6e0d1";
    sprite.fontFace = "Georgia";
    sprite.textHeight = endpointSet.has(n.id) ? 5 : 4;
    sprite.position.set(0, 8, 0);
    return sprite;
  }

  function buildGraph3d(data) {
    if (!window.Lib3D) {
      hint.textContent = "3D view could not load — chains still listed in detail panel";
      return;
    }
    virtualSet = new Set();
    for (const n of data.graph.nodes) if (n.virtual) virtualSet.add(n.id);
    graphBox.classList.add("shown");
    canvas.style.display = "none";
    if (Graph === null) {
      Graph = window.Lib3D.ForceGraph3D()(graphBox)
        .backgroundColor("#0c0e13")
        .showNavInfo(false)
        .width(window.innerWidth)
        .height(window.innerHeight)
        .nodeRelSize(3)
        .nodeOpacity(0.85)
        .nodeLabel(n => n.id)
        .nodeColor(nodeColor)
        .nodeThreeObject(nodeObject)
        .nodeThreeObjectExtend(true)
        .linkColor(l => linkOnPath(l) ? "#e8d49a" : (l.weight >= 2 ? "#4a5364" : "#30364a"))
        .linkWidth(l => linkOnPath(l) ? 2 : 0.15)
        .linkOpacity(0.32)
        .linkDirectionalParticles(l => linkOnPath(l) ? 3 : 0)
        .linkDirectionalParticleWidth(1.6)
        .linkDirectionalParticleSpeed(0.006)
        .linkDirectionalParticleColor(() => "#c9b57b")
        .onNodeClick(n => {
          if (!n.virtual) window.open("https://en.wikipedia.org/wiki/" + encodeURIComponent(n.id.replace(/ /g, "_")), "_blank");
        })
        .onLinkClick(l => {
          if (linkOnPath(l)) openEvidence(idOf(l.source), idOf(l.target));
        });
      Graph.d3Force("charge").strength(-45);
    }
    Graph.graphData(data.graph);
  }

  function repaintGraph() {
    if (!Graph) return;
    Graph.nodeColor(Graph.nodeColor());
    Graph.linkColor(Graph.linkColor());
    Graph.linkWidth(Graph.linkWidth());
    Graph.linkDirectionalParticles(Graph.linkDirectionalParticles());
    Graph.nodeThreeObject(Graph.nodeThreeObject());
  }

  function selectChain(entry, index) {
    pathNodeSet = new Set(entry.path);
    endpointSet = new Set([entry.path[0], entry.path[entry.path.length - 1]]);
    pathEdgeSet = new Set(entry.edges.map(e => keyOf(e.from, e.to)));
    repaintGraph();
    if (Graph) setTimeout(() => Graph.zoomToFit(1200, 90, n => pathNodeSet.has(n.id)), 600);

    degrees.textContent = entry.degrees;
    strengthLine.textContent = entry.degrees === 1 ? "Degree of separation" : "Degrees of separation";
    verdict.classList.add("shown");
    controls.classList.add("shown");
    legend.classList.add("shown");

    hops.innerHTML = "";
    entry.edges.forEach(function (edge, i) {
      const card = document.createElement("div");
      card.className = "hop";
      let kind = edge.weight >= 3 ? "in the news together" : (edge.weight >= 2 ? "mutual link" : "linked");
      if (edge.met === true) kind = "verified &mdash; they met";
      card.innerHTML = escapeHtml(edge.from) + "<i>&#8644;</i>" + escapeHtml(edge.to) +
        "<small>" + kind + " &middot; how?</small>";
      card.addEventListener("click", function () { openEvidence(edge.from, edge.to); });
      hops.appendChild(card);
    });
    hops.classList.add("shown");

    if (chains.children.length > index) {
      for (const other of chains.children) other.classList.remove("active");
      chains.children[index].classList.add("active");
    }
  }

  /* ---------- evidence ---------- */
  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  async function openEvidence(a, b) {
    evidence.classList.add("shown");
    eviPair.innerHTML = escapeHtml(a) + "<i>&#8644;</i>" + escapeHtml(b);
    eviBody.innerHTML = "<p class='loadingDots'>Searching deeper for how they met&hellip;</p>";
    try {
      const res = await fetch("/evidence?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b));
      const data = await res.json();
      let html = "";
      if (data.met === true) {
        html += "<p class='metBadge yes'>They knew each other personally</p>";
      } else if (data.met === false) {
        html += "<p class='metBadge no'>No record they ever met</p>";
      }
      if (data.ai && data.ai.trim() !== "") {
        for (const para of data.ai.split("\\n")) {
          if (para.trim() !== "") html += "<p>" + escapeHtml(para.trim()) + "</p>";
        }
      }
      if (data.wiki && data.wiki.length > 0) {
        html += "<h3>From Wikipedia</h3>";
        for (const q of data.wiki) {
          html += "<div class='quote'><b>" + escapeHtml(q.page) + ":</b> &ldquo;" +
                  escapeHtml(q.sentence) + "&rdquo;</div>";
        }
      }
      if (data.news && data.news.length > 0) {
        html += "<h3>In the news</h3>";
        for (const s of data.news) {
          const meta = [s.source, s.date].filter(x => x && x !== "").map(escapeHtml).join(" &middot; ");
          html += "<a href='" + encodeURI(s.link) + "' target='_blank' rel='noopener'>" +
                  escapeHtml(s.title) + (meta !== "" ? "<span class='when'>" + meta + "</span>" : "") + "</a>";
        }
      }
      if (data.sources && data.sources.length > 0) {
        html += "<h3>Sources</h3>";
        for (const s of data.sources) {
          html += "<a href='" + encodeURI(s.link) + "' target='_blank' rel='noopener'>" +
                  escapeHtml(s.title) + "</a>";
        }
      }
      if (data.virtual) {
        html += "<p class='note'>One of these people has no Wikipedia biography — this link was found by our deep search using your context.</p>";
      }
      if (html === "") html = "<p class='loadingDots'>Nothing definitive found. They are linked through their Wikipedia pages, but the record of how is thin.</p>";
      eviBody.innerHTML = html;
    } catch (err) {
      eviBody.innerHTML = "<p class='loadingDots'>The archive did not answer.</p>";
    }
  }

  /* ---------- console feed ---------- */
  function pushLine(text) {
    const line = document.createElement("div");
    line.textContent = text;
    line.className = "live";
    cons.appendChild(line);
    const kids = cons.children;
    for (let i = 0; i < kids.length - 1; i++) kids[i].className = "";
    while (cons.children.length > 8) cons.removeChild(cons.firstChild);
  }

  async function pollFeed() {
    try {
      const res = await fetch("/feed");
      const lines = await res.json();
      for (const t of lines) pushLine(t);
    } catch (e) {}
  }

  /* ---------- detail panel ---------- */
  function statRow(label, value) {
    const row = document.createElement("div");
    row.className = "stat";
    const left = document.createElement("span");
    left.textContent = label;
    const right = document.createElement("span");
    right.textContent = value;
    row.appendChild(left); row.appendChild(right);
    return row;
  }

  function renderDetail(data) {
    const s = data.stats || {};
    const opened = s.opened || [];
    counts.innerHTML = "";
    counts.appendChild(statRow("People opened", opened.length));
    counts.appendChild(statRow("Links scanned", s.linksScanned || 0));
    counts.appendChild(statRow("Rejected by title", s.screenedOut || 0));
    counts.appendChild(statRow("Titles checked", s.classified || 0));
    counts.appendChild(statRow("Turned out to be people", s.people || 0));
    counts.appendChild(statRow("Turned out not to be", s.notPeople || 0));
    counts.appendChild(statRow("People in graph", s.nodes || 0));
    counts.appendChild(statRow("Links in graph", s.edges || 0));
    counts.appendChild(statRow("Wikipedia calls", s.wikiCalls || 0));
    counts.appendChild(statRow("Web searches", s.serpCalls || 0));
    counts.appendChild(statRow("Deep searches", s.aiCalls || 0));
    counts.appendChild(statRow("Meetings verified", s.metChecks || 0));
    counts.appendChild(statRow("Failed calls", s.failedCalls || 0));

    chains.innerHTML = "";
    const paths = data.paths || [];
    if (paths.length === 0) {
      const empty = document.createElement("div");
      empty.className = "chain";
      empty.innerHTML = "<div class='names'><i>No chain of real meetings could be found.</i></div>";
      chains.appendChild(empty);
    }
    paths.forEach(function (entry, index) {
      const card = document.createElement("div");
      card.className = "chain" + (index === 0 ? " active" : "");
      const label = index === 0 ? "<b>Shortest</b>" : "Alternate " + index;
      const mutual = entry.edges.filter(function (e) { return e.weight >= 2; }).length;
      card.innerHTML =
        "<div class='top'><span>" + label + "</span><span>" + entry.degrees +
        " deg &middot; " + mutual + " mutual</span></div>" +
        "<div class='names'>" + entry.path.map(escapeHtml).join(" <i>&rarr;</i> ") + "</div>";
      card.addEventListener("click", function () { selectChain(entry, index); });
      chains.appendChild(card);
    });

    openedBox.innerHTML = "";
    for (const name of opened) {
      const line = document.createElement("div");
      line.textContent = name;
      openedBox.appendChild(line);
    }
  }

  /* ---------- flow ---------- */
  function reset() {
    mode = "idle";
    missingSide = "";
    fromCtx.value = ""; toCtx.value = "";
    pathNodeSet = new Set(); pathEdgeSet = new Set(); endpointSet = new Set();
    panel.classList.remove("hidden");
    verdict.classList.remove("shown");
    controls.classList.remove("shown");
    panelWrap.classList.remove("shown");
    evidence.classList.remove("shown");
    rescue.classList.remove("shown");
    hops.classList.remove("shown");
    legend.classList.remove("shown");
    cons.classList.remove("shown");
    graphBox.classList.remove("shown");
    canvas.style.display = "";
    cons.innerHTML = "";
    hops.innerHTML = "";
    hint.textContent = "";
    contextBox.value = "";
    fromBox.value = ""; toBox.value = ""; fromBox.focus();
  }

  async function runSearch() {
    const a = fromBox.value.trim(), b = toBox.value.trim();
    if (a === "" || b === "") { hint.textContent = "Two names are needed"; return; }
    rescue.classList.remove("shown");
    hint.textContent = "Searching the web";
    cons.innerHTML = "";
    cons.classList.add("shown");
    if (feedTimer) clearInterval(feedTimer);
    feedTimer = setInterval(pollFeed, 400);
    try {
      const url = "/search?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b) +
        "&ca=" + encodeURIComponent(fromCtx.value.trim()) + "&cb=" + encodeURIComponent(toCtx.value.trim());
      const res = await fetch(url);
      const data = await res.json();
      clearInterval(feedTimer); feedTimer = null;
      await pollFeed();
      lastData = data;
      renderDetail(data);

      if (data.status === "not_found") {
        hint.innerHTML = "No biography found for <b>" + escapeHtml(data.missing) + "</b>";
        missingSide = data.side;
        missingName.textContent = data.missing;
        rescue.classList.add("shown");
        contextBox.focus();
        controls.classList.add("shown");
      } else if (data.status === "no_path") {
        hint.textContent = "No chain found within reach — the web it explored is behind you";
        mode = "graph";
        panel.classList.add("hidden");
        buildGraph3d(data);
        controls.classList.add("shown");
        legend.classList.add("shown");
      } else {
        hint.textContent = "";
        mode = "graph";
        panel.classList.add("hidden");
        buildGraph3d(data);
        selectChain(data.paths[0], 0);
      }
    } catch (err) {
      if (feedTimer) { clearInterval(feedTimer); feedTimer = null; }
      hint.textContent = "The archive did not answer";
    }
  }

  function retryWithContext() {
    const text = contextBox.value.trim();
    if (text === "") { contextBox.focus(); return; }
    if (missingSide === "b") toCtx.value = text; else fromCtx.value = text;
    rescue.classList.remove("shown");
    runSearch();
  }

  function onKey(e) { if (e.key === "Enter") runSearch(); }
  fromBox.addEventListener("keydown", onKey);
  toBox.addEventListener("keydown", onKey);
  fromCtx.addEventListener("keydown", onKey);
  toCtx.addEventListener("keydown", onKey);
  contextBox.addEventListener("keydown", function (e) { if (e.key === "Enter") retryWithContext(); });
  retry.addEventListener("click", retryWithContext);
  dismiss.addEventListener("click", function () { rescue.classList.remove("shown"); });
  again.addEventListener("click", reset);
  showPanel.addEventListener("click", function () { panelWrap.classList.toggle("shown"); });
  closePanel.addEventListener("click", function () { panelWrap.classList.remove("shown"); });
  closeEvidence.addEventListener("click", function () { evidence.classList.remove("shown"); });
  window.addEventListener("resize", seed);
  seed(); frame();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/search":
            self.handleSearch(parsed.query)
        elif parsed.path == "/evidence":
            self.handleEvidence(parsed.query)
        elif parsed.path == "/feed":
            self.handleFeed()
        else:
            self.handlePage()

    def reply(self, body, contentType):
        try:
            self.send_response(200)
            self.send_header("Content-Type", contentType)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handlePage(self):
        self.reply(PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def handleFeed(self):
        lines = []
        while not feed.empty():
            lines.append(feed.get())
        self.reply(json.dumps(lines).encode("utf-8"), "application/json; charset=utf-8")

    def handleEvidence(self, query):
        params = parse_qs(query)
        a = params.get("a", [""])[0]
        b = params.get("b", [""])[0]
        result = evidenceBetween(a, b)
        saveCache()
        self.reply(json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")

    def handleSearch(self, query):
        params = parse_qs(query)
        nameA = params.get("a", [""])[0]
        nameB = params.get("b", [""])[0]
        contextA = params.get("ca", [""])[0]
        contextB = params.get("cb", [""])[0]
        with searchLock:
            while not feed.empty():
                feed.get()
            print("Search: " + nameA + "  ->  " + nameB)
            result = search(nameA, nameB, contextA, contextB)
            stats = result["stats"]
            detail = result["status"]
            if detail == "found":
                detail += " (" + str(result["degrees"]) + " deg, " + str(len(result["paths"])) + " chains)"
            print("   result: " + detail)
            print("   opened " + str(len(stats["opened"])) + " people, " +
                  str(stats["people"]) + " links were people, " +
                  str(stats["notPeople"]) + " were not")
            print("   " + str(stats["wikiCalls"]) + " wikipedia calls, " +
                  str(stats["serpCalls"]) + " web searches (" +
                  str(stats["aiCalls"]) + " ai answers), " +
                  str(stats["failedCalls"]) + " failures")
        self.reply(json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


def main():
    if SERPAPI_KEY == "":
        print("SERPAPI_API_KEY not set. Deep search, news checks, misspelling fixes and context lookups will not work.\n")
    loadCache()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Lelantos running at http://127.0.0.1:" + str(PORT))
    print("First searches are slow. The cache makes repeats fast and persists between runs.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Saving cache.")
        saveCache()
        server.server_close()


if __name__ == "__main__":
    main()
