"""将文档内 HTML 表格转为 Markdown，保留表头与跨行单元格关系。"""
from html.parser import HTMLParser


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = []
        self.cell = None
        self.attrs = {}
        self.header_rows = []
        self.in_thead = False
        self.row_header = False

    def handle_starttag(self, tag, attrs):
        if tag == "thead":
            self.in_thead = True
        if tag == "tr":
            self.row = []
            self.row_header = self.in_thead
        elif tag in ("td", "th"):
            self.cell, self.attrs = [], dict(attrs)
            self.row_header = self.row_header or tag == "th"
        elif self.cell is not None:
            if tag in ("br", "p", "li"):
                self.cell.append(" ")
            elif tag not in ("span", "div", "code", "strong", "b", "em", "i", "a", "ul", "ol"):
                self.cell.append(self.get_starttag_text())

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append((" ".join("".join(self.cell).split()),
                             max(1, int(self.attrs.get("colspan", 1))),
                             max(1, int(self.attrs.get("rowspan", 1)))))
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.header_rows.append(self.row_header)
        elif tag == "thead":
            self.in_thead = False


def table_to_markdown(html: str) -> str:
    parser = TableParser()
    parser.feed(html)
    if not parser.rows:
        return html
    expanded, pending = [], {}
    for cells in parser.rows:
        row, column = [], 0
        carried = dict(pending)
        pending = {key: (count - 1, value) for key, (count, value) in pending.items() if count > 1}

        def carry():
            nonlocal column
            while column in carried:
                row.append(carried[column][1])
                column += 1

        for value, colspan, rowspan in cells:
            carry()
            for _ in range(colspan):
                row.append(value)
                if rowspan > 1:
                    pending[column] = (rowspan - 1, value)
                column += 1
        carry()
        expanded.append(row)
    width = max(map(len, expanded))
    header_count = 0
    for is_header in parser.header_rows:
        if not is_header:
            break
        header_count += 1
    if header_count > 1:
        # 多层表头沿列合并，拆表后每个字段仍携带上层分组名。
        header = [" / ".join(dict.fromkeys(row[col] for row in expanded[:header_count]
                                          if col < len(row) and row[col])) for col in range(width)]
        expanded = [header] + expanded[header_count:]
    rows = ["| " + " | ".join(value.replace("|", "\\|") for value in row + [""] * (width - len(row))) + " |"
            for row in expanded]
    return "\n" + rows[0] + "\n| " + " | ".join(["---"] * width) + " |\n" + "\n".join(rows[1:]) + "\n"
