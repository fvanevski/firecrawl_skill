import sys
import os
import pytest

# Ensure our local firecrawl scripts directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from classifier import classify_target, PROFILES

def test_ecommerce_url_patterns():
    # Conforming URL structures should trigger ecommerce candidate status
    conforming_urls = [
        "http://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
        "https://example.com/product/premium-sunglasses",
        "https://example.com/p/12345",
        "https://example.com/item/wool-sweater",
        "https://example.com/shop/electronics/headphones"
    ]
    for url in conforming_urls:
        category, is_match = classify_target(url, "Test Page", "No snippet")
        assert category == "ecommerce"
        assert is_match is True

def test_ecommerce_keyword_patterns():
    # Conforming snippets and titles should trigger ecommerce classification
    category, is_match = classify_target("https://example.com/somepage", "Premium Widget", "Price: $49.99, In Stock today!")
    assert category == "ecommerce"
    assert is_match is True

    category, is_match = classify_target("https://example.com/anotherpage", "Underpants - Buy Now", "Add to cart for 15% off with sku-1002")
    assert category == "ecommerce"
    assert is_match is True

def test_forum_url_patterns():
    # Conforming discussion URL structures should trigger forum candidate status
    conforming_urls = [
        "https://news.ycombinator.com/item?id=39123456",
        "https://reddit.com/r/technology/comments/post_id",
        "https://example.com/thread/installing-virgl-on-termux",
        "https://example.com/community/general-chat"
    ]
    for url in conforming_urls:
        category, is_match = classify_target(url, "Test Page", "No snippet")
        assert category == "forum"
        assert is_match is True

def test_forum_keyword_patterns():
    # Conforming snippets and titles should trigger forum classification
    category, is_match = classify_target("https://example.com/page", "Is anyone else experiencing this?", "Posted by tech_guy 5 hours ago. 12 replies.")
    assert category == "forum"
    assert is_match is True

    category, is_match = classify_target("https://example.com/page", "Mesa compilation updates", "This is an ongoing discussion thread with 42 comments.")
    assert category == "forum"
    assert is_match is True

def test_non_conforming_fallbacks():
    # Regular encyclopedic pages should not trigger structured profiles
    fallbacks = [
        ("https://en.wikipedia.org/wiki/Web_scraping", "Web scraping - Wikipedia", "Web scraping, web harvesting, or web data extraction is data scraping used for extracting data from websites.")
    ]
    for url, title, snippet in fallbacks:
        category, is_match = classify_target(url, title, snippet)
        assert category == "editorial_markdown"
        assert is_match is False

def test_news_article_patterns():
    # Conforming news URL structures should trigger news_article candidate status
    conforming_urls = [
        "https://apnews.com/article/tech-updates-today",
        "https://www.reuters.com/world/europe/israel-stands-firm",
        "https://www.bbc.com/news/videos/cx24793rx4lo"
    ]
    for url in conforming_urls:
        category, is_match = classify_target(url, "Test News Page", "No snippet")
        assert category == "news_article"
        assert is_match is True

    # News keywords should trigger news_article classification
    category, is_match = classify_target("https://example.com/page", "Breaking Update", "Published on June 24, 2026. Reported by Reuters news desk.")
    assert category == "news_article"
    assert is_match is True

def test_media_release_patterns():
    # Conforming media release URL structures should trigger media_release candidate status
    conforming_urls = [
        "https://www.fathomentertainment.com/releases/the-story-of-everything/",
        "https://www.tbn.org/programs/praise/episodes/dr-stephen-meyer",
        "https://example.com/podcast/some-episode"
    ]
    for url in conforming_urls:
        category, is_match = classify_target(url, "Test Media Page", "No snippet")
        assert category == "media_release"
        assert is_match is True

    # Media keywords should trigger media_release classification
    category, is_match = classify_target("https://example.com/page", "Film Premiere Today", "Watch trailer and find showtimes for the new theatrical release starring famous actors.")
    assert category == "media_release"
    assert is_match is True

def test_academic_debate_patterns():
    # Conforming academic debate URL structures should trigger academic_debate candidate status
    conforming_urls = [
        "https://philpapers.org/archive/SCHSBF-2.pdf",
        "https://plato.stanford.edu/entries/ontological-arguments/",
        "https://iep.utm.edu/arguments/"
    ]
    for url in conforming_urls:
        category, is_match = classify_target(url, "Test Academic Page", "No snippet")
        assert category == "academic_debate"
        assert is_match is True

    # Academic keywords should trigger academic_debate classification
    category, is_match = classify_target("https://example.com/page", "Is the Cosmological Argument valid?", "We examine Premise 1, Premise 2, and whether the conclusion follows using a deductive syllogism.")
    assert category == "academic_debate"
    assert is_match is True

def test_schema_robustness_definitions():
    # Test that JSON Schema shapes are fully defined and robust
    for name, config in PROFILES.items():
        schema = config.get("target_schema")
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert "required" in schema
        
        # Verify defensive "null" typing is used to prevent extraction hallucination
        properties = schema["properties"]
        for prop_name, prop_def in properties.items():
            prop_type = prop_def.get("type")
            # If it has a type field, it should allow null (as a list of types) or be an array
            if isinstance(prop_type, list):
                assert "null" in prop_type
            elif isinstance(prop_type, str):
                # Unless it's an array/object which has items/properties definitions
                assert prop_type in ["array", "object"]
