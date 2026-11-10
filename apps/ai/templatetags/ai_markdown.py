import html
import markdown
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name="render_markdown")
def render_markdown(text: str) -> str:
    """
    Safely renders assistant markdown responses to HTML.
    Pre-escapes raw HTML to prevent XSS injection, then parses standard Markdown
    syntax including headings, bold/italics, lists, tables, and code blocks.
    """
    if not text:
        return ""

    # 1. Sanitize: Escape any raw HTML tags in model output
    escaped_text = html.escape(str(text))

    # 2. Render Markdown syntax
    html_output = markdown.markdown(
        escaped_text,
        extensions=["extra", "nl2br", "sane_lists"],
    )

    return mark_safe(html_output)
