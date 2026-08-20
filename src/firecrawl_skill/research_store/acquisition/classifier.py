# Automated pre-scrape candidate classification and schema selection.

import argparse
import json

from .candidate_ranking import classify_url

PROFILES = {
    "ecommerce": {
        "url_patterns": ["/product/", "/p/", "/item/", "/catalogue/", "/shop/", "/gp/product/", "/dp/"],
        "keywords": ["price:", "£", "$", "€", "in stock", "rating:", "add to cart", "buy now", "specification", "sku"],
        "target_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": ["string", "null"], "description": "The exact product title as listed at the top of the page. Return null if missing."},
                "price": {"type": ["string", "null"], "description": "The current selling price of the product, including currency symbol. Return null if unavailable."},
                "in_stock": {"type": ["boolean", "null"], "description": "Whether the product is currently in stock or available. Return null if unknown."},
                "rating": {"type": ["string", "null"], "description": "The product rating or review score (e.g., '5 stars', '4.5/5'). Return null if absent."},
            },
            "required": ["product_name"],
        },
    },
    "forum": {
        "url_patterns": ["/comments/", "/thread/", "/community/", "ycombinator.com/item", "reddit.com/r/"],
        "keywords": ["comments", "posted by", "replies", "discussion", "thread", "username", "comment_text"],
        "target_schema": {
            "type": "object",
            "properties": {
                "discussion_title": {"type": ["string", "null"], "description": "The main topic or title of the forum discussion thread. Return null if missing."},
                "original_poster": {"type": ["string", "null"], "description": "The username of the person who started the discussion. Return null if absent."},
                "comments_count": {"type": ["integer", "null"], "description": "The total number of comments or replies in the discussion. Return null if missing."},
            },
            "required": ["discussion_title"],
        },
    },
    "news_article": {
        "url_patterns": ["/news/", "/article/", "/world/", "/politics/", "/story/", "/press-release/", "/blog/", "reuters.com", "apnews.com", "bloomberg.com", "bbc.com/news", "pbs.org/newshour"],
        "keywords": ["published on", "written by", "reported by", "associated press", "reuters", "news desk", "byline", "reporting from"],
        "target_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": ["string", "null"], "description": "The main headline or title of the news article. Return null if missing."},
                "byline": {"type": ["array", "null"], "items": {"type": "string"}, "description": "List of authors, reporters, or agencies credited. Return null if absent."},
                "published_date": {"type": ["string", "null"], "description": "The date and/or time when the article was published or last updated. Return null if missing."},
                "source_outlet": {"type": ["string", "null"], "description": "The news outlet or publisher name. Return null if unknown."},
                "dateline_location": {"type": ["string", "null"], "description": "The reporting origin city/location (e.g., 'WASHINGTON'). Return null if unknown."},
                "summary": {"type": ["string", "null"], "description": "A 2-3 sentence summary of the news reported. Return null if missing."},
                "key_entities": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Important people, organizations, or nations involved. Return null if empty."},
                "quantitative_data": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {"type": "string", "description": "What the data represents (e.g., 'Defense Funding')"},
                            "value": {"type": "string", "description": "The actual value or statistic (e.g., '$1.4 billion' or '65% disapproval')"},
                            "context": {"type": "string", "description": "Brief description of the context for this value."},
                        },
                        "required": ["metric", "value"],
                    },
                    "description": "Any key statistics, polling numbers, budgets, or monetary details.",
                },
            },
            "required": ["headline", "summary"],
        },
    },
    "breaking_news": {
        "url_patterns": ["/news/", "/article/", "/world/", "/politics/", "/story/", "/press-release/", "/blog/", "reuters.com", "apnews.com", "bloomberg.com", "bbc.com/news", "pbs.org/newshour"],
        "keywords": ["published on", "written by", "reported by", "associated press", "reuters", "news desk", "byline", "reporting from"],
        "target_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": ["string", "null"], "description": "The main headline or title of the news article. Return null if missing."},
                "byline": {"type": ["array", "null"], "items": {"type": "string"}, "description": "List of authors, reporters, or agencies credited. Return null if absent."},
                "published_date": {"type": ["string", "null"], "description": "The date and/or time when the article was published or last updated. Return null if missing."},
                "source_outlet": {"type": ["string", "null"], "description": "The news outlet or publisher name. Return null if unknown."},
                "summary": {"type": ["string", "null"], "description": "A 2-3 sentence summary of the news reported. Return null if missing."},
                "substantive_content": {"type": ["string", "null"], "description": "The main substantive body text of the article. Return null if missing."},
                "key_entities": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Important people, organizations, or nations involved. Return null if empty."},
            },
            "required": ["headline", "summary"],
        },
    },
    "legislative_legal": {
        "url_patterns": ["leginfo.legislature", "congress.gov", "supremecourt.gov", "/bill/", "/law/", "/code/"],
        "keywords": ["bill text", "statute", "section", "amended", "chapter", "opinion", "court"],
        "target_schema": {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"], "description": "The title of the bill, law, or legal opinion. Return null if missing."},
                "bill_number_or_citation": {"type": ["string", "null"], "description": "The formal bill number or legal citation. Return null if missing."},
                "status": {"type": ["string", "null"], "description": "The current status of the bill or case. Return null if missing."},
                "substantive_text": {"type": ["string", "null"], "description": "The full substantive text of the legislation or legal opinion. Return null if missing."},
                "legal_context": {"type": ["string", "null"], "description": "Contextual details such as legislative history, summary, or digest. Return null if missing."},
            },
            "required": ["title", "substantive_text"],
        },
    },
    "media_release": {
        "url_patterns": ["/releases/", "/events/", "/programs/", "/movies/", "/shows/", "/podcast/", "/episodes/", "/webinar/", "/conference/"],
        "keywords": ["theatrical release", "showtimes", "executive producer", "hosted by", "keynote speaker", "watch trailer", "ticket sales"],
        "target_schema": {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"], "description": "The title of the film, book, podcast episode, or event. Return null if missing."},
                "medium_type": {"type": ["string", "null"], "description": "The format (e.g., film, book, podcast, event, press_release). Return null if unknown."},
                "release_or_event_date": {"type": ["string", "null"], "description": "The date when the media releases or when the event takes place. Return null if missing."},
                "key_participants": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Speakers, cast members, authors, interviewees, or hosts involved. Return null if empty."},
                "sponsors_or_distributors": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Organizations, studios, or publishers producing or distributing. Return null if empty."},
                "synopsis_or_summary": {"type": ["string", "null"], "description": "A summary of the media content, plot, or event agenda. Return null if missing."},
                "core_topics_or_claims": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Key themes, arguments, or topics highlighted. Return null if empty."},
                "associated_works": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Books or prior works upon which this release is based. Return null if empty."},
                "call_to_action_url": {"type": ["string", "null"], "description": "URL to watch trailer, stream, buy tickets, or register. Return null if missing."},
            },
            "required": ["title", "medium_type"],
        },
    },
    "academic_debate": {
        "url_patterns": ["/phil/", "/philosophy/", "/debate/", "/thesis/", "/arguments/", "/objections/", "philpapers.org", "plato.stanford.edu", "iep.utm.edu", "reasonablefaith.org"],
        "keywords": ["premise 1", "conclusion follows", "ontological", "cosmological", "teleological", "rebuttal", "refutation", "logical fallacy", "syllogism"],
        "target_schema": {
            "type": "object",
            "properties": {
                "argument_name": {"type": ["string", "null"], "description": "The common name of the argument/thesis. Return null if missing."},
                "logical_framework": {"type": ["string", "null"], "description": "The logic system or framework employed (e.g. S5, Bayesian). Return null if unknown."},
                "key_proponents": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Scholars/philosophers advocating for this argument. Return null if empty."},
                "formal_premises": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "e.g. 'Premise 1', 'Conclusion'"},
                            "assertion": {"type": "string", "description": "The core claim asserted."},
                        },
                        "required": ["label", "assertion"],
                    },
                    "description": "The formal step-by-step logical premises.",
                },
                "core_supportive_claims": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Main supportive evidence or rationale. Return null if empty."},
                "key_objections": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "objection_name": {"type": "string", "description": "e.g., 'Puddle Analogy'"},
                            "critics": {"type": ["array", "null"], "items": {"type": "string"}},
                            "argument_counter": {"type": "string", "description": "Explanation of the objection."},
                        },
                        "required": ["objection_name", "argument_counter"],
                    },
                    "description": "Counter-arguments and objections.",
                },
                "rebuttals": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "targeted_objection": {"type": "string", "description": "The objection being addressed."},
                            "defense_argument": {"type": "string", "description": "The defense or rebuttal offered."},
                        },
                        "required": ["targeted_objection", "defense_argument"],
                    },
                    "description": "Rebuttals raised by the proponents.",
                },
            },
            "required": ["argument_name"],
        },
    },
}


def classify_url_type(url: str, title: str = "", snippet: str = "") -> str:
    """Return the canonical structural URL classification used by ranking."""
    return classify_url(url, title, snippet).value


def classify_target(url, title="", snippet=""):
    """Apply Layer 1 profile heuristics for structured schema extraction."""
    url_lower = url.lower()
    text_to_scan = f"{title} {snippet}".lower()
    for profile_name, rules in PROFILES.items():
        if any(pattern in url_lower for pattern in rules["url_patterns"]):
            return profile_name, True
        if any(keyword in text_to_scan for keyword in rules["keywords"]):
            return profile_name, True
    return "editorial_markdown", False


def main():
    parser = argparse.ArgumentParser(
        description="Heuristic URL pre-classification for structured schema extraction."
    )
    parser.add_argument("target", help="URL to evaluate.")
    parser.add_argument("-t", "--title", default="", help="Page title.")
    parser.add_argument("-s", "--snippet", default="", help="Snippet context.")
    args = parser.parse_args()
    category, is_match = classify_target(args.target, args.title, args.snippet)
    print(json.dumps({
        "url": args.target,
        "is_candidate": is_match,
        "classified_category": category,
        "url_type": classify_url_type(args.target, args.title, args.snippet),
    }, indent=2))


if __name__ == "__main__":
    main()
