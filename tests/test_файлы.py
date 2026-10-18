"""Присланный файл раскладывается так, чтобы коуч его прочёл.

Офисные файлы собираются здесь же, вручную, из голого XML: настоящий Word
в репозитории был бы двоичным грузом, а проверяем мы ровно то, что разбор
читает, — нужные XML внутри zip.
"""

import io
import zipfile

from src.files import разобрать

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _zip(файлы: dict[str, str]) -> bytes:
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, "w") as архив:
        for имя, текст in файлы.items():
            архив.writestr(имя, текст)
    return буфер.getvalue()


def test_маркдаун_кладётся_как_есть(tmp_path):
    разбор = разобрать(tmp_path, "план.md", "# Цели\n- бег\n".encode())
    assert разбор.путь == tmp_path / "план.md"
    assert разбор.путь.read_text(encoding="utf-8") == "# Цели\n- бег\n"
    assert not разбор.вынуто


def test_текст_из_windows_перекодирован_в_utf8(tmp_path):
    разбор = разобрать(tmp_path, "заметки.txt", "Привет".encode("cp1251"))
    assert разбор.путь.read_text(encoding="utf-8") == "Привет"


def test_неизвестное_расширение_с_utf8_всё_равно_текст(tmp_path):
    разбор = разобрать(tmp_path, "конфиг.conf", "ключ = значение".encode())
    assert разбор.вид == "текст"


def test_путь_из_имени_не_выходит_из_папки(tmp_path):
    разбор = разобрать(tmp_path / "в", "../../злое.txt", b"x")
    assert разбор.путь.parent == tmp_path / "в"


def test_pdf_кладётся_как_есть(tmp_path):
    разбор = разобрать(tmp_path, "отчёт.pdf", b"%PDF-1.4 ...")
    assert разбор.вид == "PDF" and разбор.путь.read_bytes() == b"%PDF-1.4 ..."


def test_word(tmp_path):
    данные = _zip({"word/document.xml": f"""<w:document {W}><w:body>
        <w:p><w:r><w:t>Первый </w:t></w:r><w:r><w:t>абзац</w:t></w:r></w:p>
        <w:p><w:r><w:t>Второй</w:t></w:r></w:p></w:body></w:document>"""})
    разбор = разобрать(tmp_path, "план.docx", данные)
    assert разбор.вынуто and разбор.путь.name == "план.docx.txt"
    assert разбор.путь.read_text(encoding="utf-8") == "Первый абзац\nВторой\n"


def test_excel_листы_по_именам_и_общие_строки(tmp_path):
    данные = _zip({
        "xl/workbook.xml": f"""<workbook {S} {R}><sheets>
            <sheet name="Бюджет" sheetId="1" r:id="rId2"/>
            <sheet name="Итоги" sheetId="2" r:id="rId1"/></sheets></workbook>""",
        "xl/_rels/workbook.xml.rels": """<Relationships>
            <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
            <Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>""",
        "xl/sharedStrings.xml": f"<sst {S}><si><t>еда</t></si><si><t>всё</t></si></sst>",
        "xl/worksheets/sheet2.xml": f"""<worksheet {S}><sheetData>
            <row><c t="s"><v>0</v></c><c><v>500</v></c></row></sheetData></worksheet>""",
        "xl/worksheets/sheet1.xml": f"""<worksheet {S}><sheetData>
            <row><c t="s"><v>1</v></c><c t="inlineStr"><is><t>ок</t></is></c></row>
            </sheetData></worksheet>""",
    })
    текст = разобрать(tmp_path, "деньги.xlsx", данные).путь.read_text(encoding="utf-8")
    assert текст == "── Лист «Бюджет» ──\nеда\t500\n\n── Лист «Итоги» ──\nвсё\tок\n"


def test_powerpoint_слайды_по_порядку_номеров(tmp_path):
    def слайд(текст):
        return f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>{текст}</a:t></a:r></a:p></p:sld>'

    данные = _zip({
        "ppt/slides/slide10.xml": слайд("десятый"),
        "ppt/slides/slide2.xml": слайд("второй"),
    })
    текст = разобрать(tmp_path, "доклад.pptx", данные).путь.read_text(encoding="utf-8")
    assert текст.index("второй") < текст.index("десятый")


def test_openoffice(tmp_path):
    данные = _zip({
        "mimetype": "application/vnd.oasis.opendocument.text",
        "content.xml": """<office:document-content xmlns:office="o"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
            <text:h>Заголовок</text:h><text:p>Тело <text:span>текста</text:span></text:p>
            </office:document-content>""",
    })
    разбор = разобрать(tmp_path, "письмо.odt", данные)
    assert разбор.путь.read_text(encoding="utf-8") == "Заголовок\nТело текста\n"


def test_архив_отказ_с_подсказкой(tmp_path):
    разбор = разобрать(tmp_path, "фото.zip", _zip({"a.jpg": "x"}))
    assert разбор.путь is None and "распакуй" in разбор.отказ


def test_старый_doc_отказ_с_подсказкой(tmp_path):
    разбор = разобрать(tmp_path, "старое.doc", b"\xd0\xcf\x11\xe0\x00\x00 binary")
    assert разбор.путь is None and ".docx" in разбор.отказ


def test_двоичное_не_притворяется_текстом(tmp_path):
    разбор = разобрать(tmp_path, "видео.mp4", b"\x00\x00\x00\x18ftypmp42")
    assert разбор.путь is None


def test_word_рамка_не_задваивается(tmp_path):
    """Пойман на настоящем файле 18.10.2026: надпись в рамке выходила дважды."""
    рамка = "<w:txbxContent><w:p><w:r><w:t>в рамке</w:t></w:r></w:p></w:txbxContent>"
    данные = _zip({"word/document.xml": f"""<w:document {W}
        xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>
        <w:p><w:r><w:t>снаружи</w:t></w:r><w:tab/><w:r><w:t>дальше</w:t></w:r><w:r>
          <mc:AlternateContent><mc:Choice>{рамка}</mc:Choice>
          <mc:Fallback>{рамка}</mc:Fallback></mc:AlternateContent></w:r></w:p>
        </w:body></w:document>"""})
    текст = разобрать(tmp_path, "схема.docx", данные).путь.read_text(encoding="utf-8")
    assert текст.count("в рамке") == 1
    assert "снаружи\tдальше" in текст


def test_пустой_файл_отказ(tmp_path):
    assert разобрать(tmp_path, "новый.txt", b"").отказ == "он пустой"
