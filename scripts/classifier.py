#!/usr/bin/env python3
# classifier.py — Automated pre-scrape candidate classification and schema selection
#
# Usage:
#   python3 classifier.py <scratch_dir_or_meta_json> [--schema-profile ecommerce|forum]
#

import os
import sys
import json
import argparse

# ── Heuristic Profiles ──────────────────────────────────────────────────────
PROFILES = {
    "ecommerce": {
        "url_patterns": ["/product/", "/p/", "/item/", "/catalogue/", "/shop/", "/gp/product/", "/dp/"],
        "keywords": ["price:", "£", "$", "€", "in stock", "rating:", "add to cart", "buy now", "specification", "sku"],
        "target_schema": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": ["string", "null"],
                    "description": "The exact product title as listed at the top of the page. Return null if missing."
                },
                "price": {
                    "type": ["string", "null"],
                    "description": "The current selling price of the product, including currency symbol. Return null if unavailable."
                },
                "in_stock": {
                    "type": ["boolean", "null"],
                    "description": "Whether the product is currently in stock or available. Return null if unknown."
                },
                "rating": {
                    "type": ["string", "null"],
                    "description": "The product rating or review score (e.g., '5 stars', '4.5/5'). Return null if absent."
                }
            },
            "required": ["product_name"]
        }
    },
    "forum": {
        "url_patterns": ["/comments/", "/thread/", "/community/", "ycombinator.com/item", "reddit.com/r/"],
        "keywords": ["comments", "posted by", "replies", "discussion", "thread", "username", "comment_text"],
        "target_schema": {
            "type": "object",
            "properties": {
                "discussion_title": {
                    "type": ["string", "null"],
                    "description": "The main topic or title of the forum discussion thread. Return null if missing."
                },
                "original_poster": {
                    "type": ["string", "null"],
                    "description": "The username of the person who started the discussion. Return null if absent."
                },
                "comments_count": {
                    "type": ["integer", "null"],
                    "description": "The total number of comments or replies in the discussion. Return null if missing."
                }
            },
            "required": ["discussion_title"]
        }
    },
    "news_article": {
        "url_patterns": [
            "/news/", "/article/", "/world/", "/politics/", "/story/", 
            "/press-release/", "/blog/", "reuters.com", "apnews.com", 
            "bloomberg.com", "bbc.com/news", "pbs.org/newshour"
        ],
        "keywords": [
            "published on", "written by", "reported by", "associated press", 
            "reuters", "news desk", "byline", "reporting from"
        ],
        "target_schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": ["string", "null"],
                    "description": "The main headline or title of the news article. Return null if missing."
                },
                "byline": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "List of authors, reporters, or agencies credited. Return null if absent."
                },
                "published_date": {
                    "type": ["string", "null"],
                    "description": "The date and/or time when the article was published or last updated. Return null if missing."
                },
                "source_outlet": {
                    "type": ["string", "null"],
                    "description": "The news outlet or publisher name. Return null if unknown."
                },
                "dateline_location": {
                    "type": ["string", "null"],
                    "description": "The reporting origin city/location (e.g., 'WASHINGTON'). Return null if unknown."
                },
                "summary": {
                    "type": ["string", "null"],
                    "description": "A 2-3 sentence summary of the news reported. Return null if missing."
                },
                "key_entities": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Important people, organizations, or nations involved. Return null if empty."
                },
                "quantitative_data": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "description": "What the data represents (e.g., 'Defense Funding')"
                            },
                            "value": {
                                "type": "string",
                                "description": "The actual value or statistic (e.g., '$1.4 billion' or '65% disapproval')"
                            },
                            "context": {
                                "type": "string",
                                "description": "Brief description of the context for this value."
                            }
                        },
                        "required": ["metric", "value"]
                    },
                    "description": "Any key statistics, polling numbers, budgets, or monetary details."
                }
            },
            "required": ["headline", "summary"]
        }
    },
    "breaking_news": {
        "url_patterns": [
            "/news/", "/article/", "/world/", "/politics/", "/story/", 
            "/press-release/", "/blog/", "reuters.com", "apnews.com", 
            "bloomberg.com", "bbc.com/news", "pbs.org/newshour"
        ],
        "keywords": [
            "published on", "written by", "reported by", "associated press", 
            "reuters", "news desk", "byline", "reporting from"
        ],
        "target_schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": ["string", "null"],
                    "description": "The main headline or title of the news article. Return null if missing."
                },
                "byline": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "List of authors, reporters, or agencies credited. Return null if absent."
                },
                "published_date": {
                    "type": ["string", "null"],
                    "description": "The date and/or time when the article was published or last updated. Return null if missing."
                },
                "source_outlet": {
                    "type": ["string", "null"],
                    "description": "The news outlet or publisher name. Return null if unknown."
                },
                "summary": {
                    "type": ["string", "null"],
                    "description": "A 2-3 sentence summary of the news reported. Return null if missing."
                },
                "substantive_content": {
                    "type": ["string", "null"],
                    "description": "The main substantive body text of the article. Return null if missing."
                },
                "key_entities": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Important people, organizations, or nations involved. Return null if empty."
                }
            },
            "required": ["headline", "summary"]
        }
    },
    "legislative_legal": {
        "url_patterns": [
            "leginfo.legislature", "congress.gov", "supremecourt.gov", "/bill/", "/law/", "/code/"
        ],
        "keywords": [
            "bill text", "statute", "section", "amended", "chapter", "opinion", "court"
        ],
        "target_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": ["string", "null"],
                    "description": "The title of the bill, law, or legal opinion. Return null if missing."
                },
                "bill_number_or_citation": {
                    "type": ["string", "null"],
                    "description": "The formal bill number or legal citation. Return null if missing."
                },
                "status": {
                    "type": ["string", "null"],
                    "description": "The current status of the bill or case. Return null if missing."
                },
                "substantive_text": {
                    "type": ["string", "null"],
                    "description": "The full substantive text of the legislation or legal opinion. Return null if missing."
                },
                "legal_context": {
                    "type": ["string", "null"],
                    "description": "Contextual details such as legislative history, summary, or digest. Return null if missing."
                }
            },
            "required": ["title", "substantive_text"]
        }
    },
    "media_release": {
        "url_patterns": [
            "/releases/", "/events/", "/programs/", "/movies/", 
            "/shows/", "/podcast/", "/episodes/", "/webinar/", "/conference/"
        ],
        "keywords": [
            "theatrical release", "showtimes", "executive producer", 
            "hosted by", "keynote speaker", "watch trailer", "ticket sales"
        ],
        "target_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": ["string", "null"],
                    "description": "The title of the film, book, podcast episode, or event. Return null if missing."
                },
                "medium_type": {
                    "type": ["string", "null"],
                    "description": "The format (e.g., film, book, podcast, event, press_release). Return null if unknown."
                },
                "release_or_event_date": {
                    "type": ["string", "null"],
                    "description": "The date when the media releases or when the event takes place. Return null if missing."
                },
                "key_participants": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Speakers, cast members, authors, interviewees, or hosts involved. Return null if empty."
                },
                "sponsors_or_distributors": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Organizations, studios, or publishers producing or distributing. Return null if empty."
                },
                "synopsis_or_summary": {
                    "type": ["string", "null"],
                    "description": "A summary of the media content, plot, or event agenda. Return null if missing."
                },
                "core_topics_or_claims": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Key themes, arguments, or topics highlighted. Return null if empty."
                },
                "associated_works": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Books or prior works upon which this release is based. Return null if empty."
                },
                "call_to_action_url": {
                    "type": ["string", "null"],
                    "description": "URL to watch trailer, stream, buy tickets, or register. Return null if missing."
                }
            },
            "required": ["title", "medium_type"]
        }
    },
    "academic_debate": {
        "url_patterns": [
            "/phil/", "/philosophy/", "/debate/", "/thesis/", 
            "/arguments/", "/objections/", "philpapers.org", 
            "plato.stanford.edu", "iep.utm.edu", "reasonablefaith.org"
        ],
        "keywords": [
            "premise 1", "conclusion follows", "ontological", "cosmological", 
            "teleological", "rebuttal", "refutation", "logical fallacy", "syllogism"
        ],
        "target_schema": {
            "type": "object",
            "properties": {
                "argument_name": {
                    "type": ["string", "null"],
                    "description": "The common name of the argument/thesis. Return null if missing."
                },
                "logical_framework": {
                    "type": ["string", "null"],
                    "description": "The logic system or framework employed (e.g. S5, Bayesian). Return null if unknown."
                },
                "key_proponents": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Scholars/philosophers advocating for this argument. Return null if empty."
                },
                "formal_premises": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "e.g. 'Premise 1', 'Conclusion'"},
                            "assertion": {"type": "string", "description": "The core claim asserted."}
                        },
                        "required": ["label", "assertion"]
                    },
                    "description": "The formal step-by-step logical premises."
                },
                "core_supportive_claims": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Main supportive evidence or rationale. Return null if empty."
                },
                "key_objections": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "objection_name": {"type": "string", "description": "e.g., 'Puddle Analogy'"},
                            "critics": {"type": ["array", "null"], "items": {"type": "string"}},
                            "argument_counter": {"type": "string", "description": "Explanation of the objection."}
                        },
                        "required": ["objection_name", "argument_counter"]
                    },
                    "description": "Counter-arguments and objections."
                },
                "rebuttals": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "targeted_objection": {"type": "string", "description": "The objection being addressed."},
                            "defense_argument": {"type": "string", "description": "The defense or rebuttal offered."}
                        },
                        "required": ["targeted_objection", "defense_argument"]
                    },
                    "description": "Rebuttals raised by the proponents."
                }
            },
            "required": ["argument_name"]
        }
    }
}

def classify_target(url, title="", snippet=""):
    """
    Applies Layer 1 static heuristics to classify a target source.
    Returns: (category, profile_matched)
    """
    url_lower = url.lower()
    text_to_scan = f"{title} {snippet}".lower()
    
    # Check each profile in order of preference
    for profile_name, rules in PROFILES.items():
        # Match URL patterns
        if any(pat in url_lower for pat in rules["url_patterns"]):
            return profile_name, True
            
        # Match high-confidence keywords in snippet or title
        if any(kw in text_to_scan for kw in rules["keywords"]):
            return profile_name, True
            
    # Default fallback to unstructured Markdown
    return "editorial_markdown", False

def process_directory(directory, chosen_profile=None):
    """
    Scans a search scratch directory (containing _meta.json or _search.json),
    classifies all found URLs, and compiles a Selective Scrape Plan.
    """
    meta_path = os.path.join(directory, "_meta.json")
    search_path = os.path.join(directory, "_search.json")
    
    source_file = None
    if os.path.exists(meta_path):
        source_file = meta_path
    elif os.path.exists(search_path):
        source_file = search_path
        
    if not source_file:
        print(f"❌ Error: No valid metadata file (_meta.json or _search.json) found in {directory}", file=sys.stderr)
        sys.exit(1)
        
    try:
        with open(source_file) as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Error reading metadata file: {e}", file=sys.stderr)
        sys.exit(1)
        
    results = data.get("results", [])
    if not results and "queries_executed" in data:
        # It's a master smart search directory - let's gather all query results
        for q in data["queries_executed"]:
            results.extend(q.get("metadata", {}).get("results", []))
            
    plan = {
        "source_directory": directory,
        "total_urls_evaluated": len(results),
        "candidates": [],
        "fallbacks": []
    }
    
    print(f"\nEvaluating {len(results)} URLs for schema match in: {directory}...", file=sys.stderr)
    
    for item in results:
        url = item.get("url")
        title = item.get("title", "")
        snippet = item.get("snippet", item.get("preview_head", ""))
        
        category, is_match = classify_target(url, title, snippet)
        
        # If the user specified a specific profile, only flag matches for that profile
        is_candidate = is_match
        if chosen_profile and category != chosen_profile:
            is_candidate = False
            
        record = {
            "url": url,
            "title": title,
            "snippet": snippet[:120] + "..." if len(snippet) > 120 else snippet,
            "classified_category": category
        }
        
        if is_candidate:
            record["matched_profile"] = category
            plan["candidates"].append(record)
        else:
            plan["fallbacks"].append(record)
            
    # Write plan to a file
    plan_path = os.path.join(directory, "_scrape_plan.json")
    with open(plan_path, "w") as pf:
        json.dump(plan, pf, indent=2)
        
    return plan, plan_path

def main():
    parser = argparse.ArgumentParser(
        description="classifier.py — Heuristic URL pre-classification for structured schema extraction."
    )
    parser.add_argument("target", help="Directory containing _meta.json/_search.json, or a specific URL to evaluate.")
    parser.add_argument("-p", "--profile", choices=list(PROFILES.keys()), help="Target schema profile to filter candidates.")
    parser.add_argument("-u", "--url", action="store_true", help="Evaluate target as a raw URL instead of a directory.")
    parser.add_argument("-t", "--title", default="", help="Page title (used with --url).")
    parser.add_argument("-s", "--snippet", default="", help="Snippet context (used with --url).")
    
    args = parser.parse_args()
    
    if args.url:
        category, is_match = classify_target(args.target, args.title, args.snippet)
        result = {
            "url": args.target,
            "is_candidate": is_match,
            "classified_category": category
        }
        print(json.dumps(result, indent=2))
        sys.exit(0)
        
    if os.path.isdir(args.target):
        plan, plan_path = process_directory(args.target, args.profile)
        
        print("\n\033[1;32m=== Selective Scrape Plan Generated ===\033[0m")
        print(f"Plan file: \033[1;36m{plan_path}\033[0m")
        print(f"Total evaluated: {plan['total_urls_evaluated']}")
        print(f"  ↳ \033[1;32mCandidates (Schema)\033[0m: {len(plan['candidates'])}")
        print(f"  ↳ \033[1;33mFallbacks (Markdown)\033[0m: {len(plan['fallbacks'])}")
        
        if plan['candidates']:
            print("\n\033[1mCandidates identified:\033[0m")
            for c in plan['candidates']:
                print(f"  - \033[1;36m[{c['classified_category'].upper()}]\033[0m {c['title']} ({c['url'][:50]}...)")
        else:
            print("\nNo candidates matching structured profiles were found. Standard markdown recommended.")
            
        sys.exit(0)
    else:
        print(f"❌ Error: Target '{args.target}' is not a directory. Use --url to evaluate raw URLs.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
