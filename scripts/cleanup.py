import re

def clean_markdown(content):
    if not content:
        return ""

    # Normalize line endings
    content = content.replace("\r\n", "\n")

    # Strip HTML comments
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    lines = content.split("\n")
    cleaned_lines = []
    in_code_block = False

    # Regex patterns for various boilerplate elements
    cookie_patterns = [
        re.compile(r'\buse cookies\b', re.I),
        re.compile(r'\bcookie policy\b', re.I),
        re.compile(r'\baccept (all )?cookies\b', re.I),
        re.compile(r'\bprivacy preference\b', re.I),
        re.compile(r'\bmanage consent\b', re.I),
        re.compile(r'\bcookie settings\b', re.I),
    ]

    navigation_patterns = [
        re.compile(r'^skip to (main )?content$', re.I),
        re.compile(r'^toggle navigation$', re.I),
        re.compile(r'^menu$', re.I),
        re.compile(r'^navigation$', re.I),
        re.compile(r'^back to top$', re.I),
        re.compile(r'^go to home$', re.I),
        re.compile(r'^site map$', re.I),
    ]

    social_patterns = [
        re.compile(r'\bshare on (facebook|twitter|linkedin|reddit|pinterest|pocket|whatsapp)\b', re.I),
        re.compile(r'^follow (us|me) on\b', re.I),
    ]

    misc_boilerplate = [
        re.compile(r'^sign in to your account$', re.I),
        re.compile(r'^sign (in|up|out)$', re.I),
        re.compile(r'^log (in|out)$', re.I),
        re.compile(r'^create (an? )?free account$', re.I),
        re.compile(r'^subscribe to (our )?newsletter$', re.I),
        re.compile(r'^all rights reserved\.?$', re.I),
        re.compile(r'^copyright © \d{4}', re.I),
        re.compile(r'^terms of (service|use)$', re.I),
        re.compile(r'^privacy policy$', re.I),
    ]

    for line in lines:
        stripped = line.strip()

        # Handle code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(line)
            continue

        if in_code_block:
            cleaned_lines.append(line)
            continue

        if not stripped:
            cleaned_lines.append("")
            continue

        is_boilerplate = False

        # Match simple boilerplate patterns for short lines
        line_len = len(stripped)
        if line_len < 150:
            if any(pat.search(stripped) for pat in cookie_patterns):
                is_boilerplate = True
            elif any(pat.search(stripped) for pat in social_patterns):
                is_boilerplate = True

        if line_len < 100:
            if any(pat.search(stripped) for pat in navigation_patterns):
                is_boilerplate = True
            elif any(pat.search(stripped) for pat in misc_boilerplate):
                is_boilerplate = True

        # Check for lines containing mostly links (link-to-text ratio) or navigation blocks
        if not is_boilerplate and line_len < 200:
            # Find all markdown links
            links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', stripped)
            if links:
                # Text remaining after removing links
                text_rem = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', '', stripped).strip()
                # Strip out common lists or table structures from remaining text
                text_rem_clean = re.sub(r'[\*\-\|#\s\•\·]+', '', text_rem).strip()
                
                # If there's very little plain text compared to links, it's likely boilerplate/navigation
                if len(text_rem_clean) < 5:
                    nav_keywords = {
                        "home", "about", "contact", "pricing", "blog", "careers", "features", 
                        "privacy", "terms", "cookies", "login", "register", "sign in", "sign up",
                        "facebook", "twitter", "linkedin", "instagram", "youtube", "github",
                        "next", "previous", "prev", "search", "subscribe", "newsletter", "terms of use"
                    }
                    link_texts = [l[0].lower().strip() for l in links]
                    if any(lt in nav_keywords or not lt for lt in link_texts) or len(links) >= 3:
                        is_boilerplate = True

        if not is_boilerplate:
            # Clean tracking query parameters from markdown links
            def clean_link(match):
                anchor = match.group(1)
                url = match.group(2)

                # Skip javascript and simple anchor links
                if url.startswith("javascript:") or url.startswith("#"):
                    return anchor

                # Filter out tracking params
                if "?" in url:
                    base, query = url.split("?", 1)
                    params = query.split("&")
                    filtered_params = []
                    for p in params:
                        if "=" in p:
                            k, v = p.split("=", 1)
                            if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "ref_", "spm", "fbclid", "gclid"}:
                                filtered_params.append(p)
                        else:
                            filtered_params.append(p)
                    if filtered_params:
                        url = base + "?" + "&".join(filtered_params)
                    else:
                        url = base

                return f"[{anchor}]({url})"

            line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', clean_link, line)

            # Simplify image markdown links to plain brackets [alt text] to keep it clean
            def clean_image(match):
                alt = match.group(1).strip()
                if alt:
                    return f"[{alt}]"
                return ""
            
            line = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', clean_image, line)

            cleaned_lines.append(line)

    # Collapse consecutive blank lines
    final_lines = []
    consecutive_empty = 0
    for line in cleaned_lines:
        if not line.strip():
            consecutive_empty += 1
            if consecutive_empty <= 1:
                final_lines.append("")
        else:
            consecutive_empty = 0
            final_lines.append(line)

    return "\n".join(final_lines).strip()
