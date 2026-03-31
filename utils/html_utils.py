from bs4 import BeautifulSoup
from bs4.formatter import HTMLFormatter
from flask import current_app, url_for
from utils.image_utils import fetch_and_cache_image
from utils.debug_utils import debug_print
import copy
import hashlib
import re
import html


class URLAwareHTMLFormatter(HTMLFormatter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def escape(self, string):
        """
        Escape special characters in the given string or list of strings.
        """
        if isinstance(string, list):
            return [html.escape(str(item), quote=True) for item in string]
        elif string is None:
            return ""
        else:
            return html.escape(str(string), quote=True)

    def attributes(self, tag):
        for key, val in tag.attrs.items():
            if key in ["href", "src"]:  # Don't escape URL attributes
                yield key, val
            else:
                yield key, self.escape(val)


def transcode_content(content):
    """
    Convert HTTPS to HTTP in CSS or JavaScript content
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    # Simple pattern to match URLs in both CSS and JS
    patterns = [
        (r"""url\(['"]?(https://[^)'"]+)['"]?\)""", r"url(\1)"),  # CSS url()
        (r'"https://', '"http://'),  # Double-quoted URLs
        (r"'https://", "'http://"),  # Single-quoted URLs
        (r"https://", "http://"),  # Unquoted URLs
    ]

    for pattern, replacement in patterns:
        content = re.sub(
            pattern,
            lambda m: replacement.replace(
                r"\1",
                (
                    m.group(1).replace("https://", "http://")
                    if len(m.groups()) > 0
                    else ""
                ),
            ),
            content,
        )

    return content.encode("utf-8")


def transcode_html(
    html,
    url=None,
    whitelisted_domains=None,
    simplify_html=False,
    tags_to_unwrap=None,
    tags_to_strip=None,
    attributes_to_strip=None,
    convert_characters=False,
    conversion_table=None,
    resize_images=True,
    max_image_width=512,
    max_image_height=342,
    convert_images=True,
    convert_images_to=None,
    dithering_algorithm="FLOYDSTEINBERG",
):
    """
    Uses BeautifulSoup to transcode payloads of the text/html content type
    """

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    # Handle character conversion regardless of whitelist status
    if convert_characters:
        for key, replacement in conversion_table.items():
            if isinstance(replacement, bytes):
                replacement = replacement.decode("utf-8")
            html = html.replace(key, replacement)

    soup = BeautifulSoup(html, "html.parser")

    # Contents of <pre> tags should always use HTML entities
    for tag in soup.find_all(["pre"]):
        tag.replace_with(str(tag))

    # Always convert HTTPS to HTTP regardless of whitelist status
    for tag in soup(["link", "script", "img", "a", "iframe"]):
        # Handle src attributes
        if "src" in tag.attrs:
            if tag["src"].startswith("https://"):
                tag["src"] = tag["src"].replace("https://", "http://")
            elif tag["src"].startswith("//"):  # Handle protocol-relative URLs
                tag["src"] = "http:" + tag["src"]

        # Handle href attributes
        if "href" in tag.attrs:
            if tag["href"].startswith("https://"):
                tag["href"] = tag["href"].replace("https://", "http://")
            elif tag["href"].startswith("//"):  # Handle protocol-relative URLs
                tag["href"] = "http:" + tag["href"]

    # Check if domain is whitelisted
    is_whitelisted = False
    if url:
        from urllib.parse import urlparse

        domain = urlparse(url).netloc
        is_whitelisted = any(
            domain.endswith(whitelisted) for whitelisted in whitelisted_domains
        )

    # Only perform tag/attribute stripping if the domain is not whitelisted and SIMPLIFY_HTML is True
    if simplify_html and not is_whitelisted:
        for tag in soup(tags_to_unwrap):
            tag.unwrap()
        for tag in soup(tags_to_strip):
            tag.decompose()
        for tag in soup():
            for attr in attributes_to_strip:
                if attr in tag.attrs:
                    del tag[attr]

    # Always handle meta refresh tags
    for tag in soup.find_all("meta", attrs={"http-equiv": "refresh"}):
        if "content" in tag.attrs and "https://" in tag["content"]:
            tag["content"] = tag["content"].replace("https://", "http://")

    # Always handle CSS with inline URLs
    for tag in soup.find_all(["style", "link"]):
        if tag.string:
            tag.string = tag.string.replace("https://", "http://")

    # Handle inline SVGs - first pass
    # If any SVG has a <use href="#id"> or <use xlink:href="#id">, find the
    # matching <symbol id="id"> elsewhere on the page and inline its contents.
    # If the symbol defines a viewBox, copy it to the parent <svg>.
    for use_tag in soup.find_all("use"):
        attrs = use_tag.attrs
        if "href" in attrs:
            ref_attr = "href"
        elif "xlink:href" in attrs:
            ref_attr = "xlink:href"
        else:
            continue
        symbol_id = use_tag[ref_attr].lstrip("#")
        symbol_tag = soup.find("symbol", {"id": symbol_id})
        if not symbol_tag:
            continue
        if (
            "viewBox" in symbol_tag.attrs
            and use_tag.parent.name == "svg"
            and "viewBox" not in use_tag.parent.attrs
        ):
            use_tag.parent["viewBox"] = symbol_tag["viewBox"]
        symbol_tag_copy = copy.copy(symbol_tag)
        use_tag.replace_with(symbol_tag_copy)
        symbol_tag_copy.unwrap()

    # Handle inline SVGs - second pass
    # Convert each <svg> to a cached image (GIF or other configured format)
    # and replace it with an <img> tag pointing to the proxy's cache endpoint.
    if convert_images_to is None:
        convert_images_to = "gif"
    for tag in soup.find_all("svg"):
        svg_attrs = tag.attrs

        # Ensure height/width are set so the <img> replacement has correct dimensions
        if "height" not in svg_attrs and "viewBox" in svg_attrs:
            view_box = svg_attrs["viewBox"].split()
            tag["height"] = view_box[3]
        if "width" not in svg_attrs and "viewBox" in svg_attrs:
            view_box = svg_attrs["viewBox"].split()
            tag["width"] = view_box[2]

        fake_url = hashlib.md5(str(tag).encode()).hexdigest()
        fetch_and_cache_image(
            fake_url,
            str(tag).encode("utf-8"),
            resize=resize_images,
            max_width=max_image_width,
            max_height=max_image_height,
            convert=convert_images,
            convert_to=convert_images_to,
            dithering=dithering_algorithm,
            hash_url=False,
        )
        extension = convert_images_to.lower() if convert_images else "gif"
        relative_url = url_for("serve_cached_image", filename=f"{fake_url}.{extension}")
        img_url = f"http://{current_app.config['MACPROXY_HOST_AND_PORT']}{relative_url}"
        debug_print(f"Replaced inline SVG with cached image: {img_url}")

        img_attrs = {"src": img_url}
        if "height" in svg_attrs:
            img_attrs["height"] = svg_attrs["height"]
        if "width" in svg_attrs:
            img_attrs["width"] = svg_attrs["width"]
        img = soup.new_tag("img", **img_attrs)
        tag.replace_with(img)

    # Use the custom formatter when converting the soup back to a string
    html = soup.decode(formatter=URLAwareHTMLFormatter())

    html = html.replace("<br/>", "<br>")
    html = html.replace("<hr/>", "<hr>")

    # Ensure the output is properly encoded
    html_bytes = html.encode("utf-8")

    return html_bytes
