"""A tiny DOM over html.parser: enough to find elements by tag and class.

LinkedIn's guest pages are server-rendered with stable BEM-style class names
(base-search-card__title, description__job-criteria-item), so class lookup
is all the parsing needs. Stdlib only.
"""

from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "tr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def iter(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def find_all(self, tag=None, cls=None, **attrs):
        for n in self.iter():
            if tag and n.tag != tag:
                continue
            if cls and cls not in n.classes:
                continue
            if any(n.attrs.get(k.replace("_", "-")) != v for k, v in attrs.items()):
                continue
            yield n

    def find(self, tag=None, cls=None, **attrs):
        return next(self.find_all(tag, cls, **attrs), None)

    def text(self):
        """Visible text, whitespace collapsed."""
        return " ".join(self._raw_text().split())

    def block_text(self):
        """Text with line breaks kept at <br> and block elements; for descriptions."""
        out = []
        self._blocks(out)
        lines = [" ".join(line.split()) for line in "".join(out).split("\n")]
        text = "\n".join(lines)
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()

    def _raw_text(self):
        return "".join(c if isinstance(c, str) else c._raw_text() for c in self.children)

    def _blocks(self, out):
        for c in self.children:
            if isinstance(c, str):
                out.append(c.replace("\n", " "))
            elif c.tag == "br":
                out.append("\n")
            else:
                block = c.tag in BLOCK
                if block:
                    out.append("\n")
                if c.tag == "li":
                    out.append("• ")
                c._blocks(out)
                if block:
                    out.append("\n")


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        # Close up to the nearest open element with this tag; ignore strays.
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if self.cur.tag not in ("script", "style"):
            self.cur.children.append(data)


def parse(html):
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root
