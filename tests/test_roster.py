"""명단 CSV 읽기(teamapp/roster.py) 테스트."""
import dataclasses
import unittest
from pathlib import Path

from teamapp.roster import RosterError, RosterRow, parse_roster

DATA_DIR = Path(__file__).parent / "data"
HEADER = "student_no,name,main_role,sub_roles"


def csv_bytes(*lines, bom=False, eol="\n"):
    """머리글 없이 줄만 받아 CSV 바이트를 만든다(테스트용)."""
    text = eol.join(lines) + eol
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def with_header(*rows, **kwargs):
    return csv_bytes(HEADER, *rows, **kwargs)


class PracticeFilesTest(unittest.TestCase):
    def read(self, name):
        return parse_roster((DATA_DIR / name).read_bytes())

    def test_reads_practice_files(self):
        easy = self.read("쉬움.csv")
        self.assertEqual(len(easy), 8)
        self.assertEqual(
            easy[0],
            RosterRow("2026101", "학생1(PL;RW)", "기획자", ("보고서 작성자",)),
        )
        by_name = {row.name: row for row in easy}
        self.assertEqual(by_name["학생5(WP)"].sub_roles, ())
        self.assertEqual(
            by_name["학생8(TT;RW;PT)"].sub_roles, ("보고서 작성자", "발표자")
        )
        self.assertEqual(len(self.read("중간.csv")), 12)
        self.assertEqual(len(self.read("어려움.csv")), 16)

    def test_practice_files_use_only_known_roles(self):
        from teamapp.rules import ROLES

        for name in ("쉬움.csv", "중간.csv", "어려움.csv"):
            for row in self.read(name):
                with self.subTest(file=name, student_no=row.student_no):
                    self.assertIn(row.main_role, ROLES)
                    self.assertTrue(set(row.sub_roles) <= set(ROLES))
                    self.assertNotIn(row.main_role, row.sub_roles)


class ParseTest(unittest.TestCase):
    def test_tolerates_blank_lines_crlf_and_spaces(self):
        data = (
            "student_no,name,main_role,sub_roles\r\n"
            " 2026001 , 가나다 ,기획자, 테스터 ; 발표자 \r\n"
            "\r\n"
        ).encode("utf-8")  # BOM 없음
        rows = parse_roster(data)
        self.assertEqual(
            rows, [RosterRow("2026001", "가나다", "기획자", ("테스터", "발표자"))]
        )

    def test_header_cells_are_stripped(self):
        data = " student_no , name ,main_role, sub_roles \n2026001,가,기획자,\n".encode()
        self.assertEqual(len(parse_roster(data)), 1)

    def test_bom_is_optional(self):
        row = "2026001,가,기획자,"
        self.assertEqual(
            parse_roster(with_header(row, bom=True)),
            parse_roster(with_header(row, bom=False)),
        )

    def test_sub_role_equal_to_main_is_dropped_and_repeats_are_merged(self):
        rows = parse_roster(
            with_header(
                "1,가,기획자,기획자;테스터",
                "2,나,기획자,테스터;테스터",
                "3,다,기획자,기획자;테스터;발표자",  # 주 역할을 빼면 2개
                "4,라,기획자,테스터;발표자;테스터",  # 겹침을 줄이면 2개
            )
        )
        self.assertEqual(
            [r.sub_roles for r in rows],
            [("테스터",), ("테스터",), ("테스터", "발표자"), ("테스터", "발표자")],
        )

    def test_empty_sub_role_items_are_ignored(self):
        rows = parse_roster(with_header("1,가,기획자,;테스터;", "2,나,기획자, ; "))
        self.assertEqual([r.sub_roles for r in rows], [("테스터",), ()])

    def test_cells_in_quotes_may_contain_commas(self):
        rows = parse_roster(with_header('1,"가, 나",기획자,'))
        self.assertEqual(rows[0].name, "가, 나")

    def test_keeps_file_order(self):
        rows = parse_roster(with_header("3,다,기획자,", "1,가,웹 프로그래머,", "2,나,테스터,"))
        self.assertEqual([r.student_no for r in rows], ["3", "1", "2"])

    def test_row_is_frozen(self):
        row = parse_roster(with_header("1,가,기획자,"))[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.name = "바꿈"


class ErrorTest(unittest.TestCase):
    def assert_error(self, data, message):
        with self.assertRaises(RosterError) as ctx:
            parse_roster(data)
        self.assertEqual(str(ctx.exception), message)

    def test_errors(self):
        cases = [
            ("머리글이 다름",
             csv_bytes("id,name,main_role,sub_roles", "2026001,가,기획자,"),
             "머리글은 student_no,name,main_role,sub_roles 이어야 합니다"),
            ("열이 3개",
             with_header("2026001,가,기획자"),
             "2번째 줄: 열이 4개가 아닙니다"),
            ("모르는 역할",
             with_header("2026001,가,디자이너,"),
             "2번째 줄: 모르는 역할 '디자이너'"),
            ("보조 역할 3개",
             with_header("2026001,가,기획자,웹 프로그래머;테스터;발표자"),
             "2번째 줄: 보조 역할은 2개까지입니다"),
            ("학번 중복",
             with_header("2026001,가,기획자,", "2026001,나,테스터,"),
             "3번째 줄: 학번 2026001이 겹칩니다"),
            ("학번 admin",
             with_header("admin,가,기획자,"),
             "2번째 줄: admin은 학번으로 쓸 수 없습니다"),
            ("학생이 없음",
             with_header(),
             "명단에 학생이 없습니다"),
            ("UTF-8이 아님",
             HEADER.encode() + b"\n" + "2026001,가나다,기획자,".encode("cp949"),
             "UTF-8 CSV 파일이 아닙니다"),
        ]
        for label, data, message in cases:
            with self.subTest(label):
                self.assert_error(data, message)

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(RosterError, ValueError))
        self.assertEqual(str(RosterError("메시지")), "메시지")

    def test_empty_inputs_have_no_students(self):
        for label, data in [
            ("빈 파일", b""),
            ("BOM만", b"\xef\xbb\xbf"),
            ("빈 줄만", b"\n\r\n  \n"),
            ("머리글 뒤에 빈 줄만", with_header("", " , , ,", "")),
        ]:
            with self.subTest(label):
                self.assert_error(data, "명단에 학생이 없습니다")

    def test_empty_cells(self):
        self.assert_error(with_header(" ,가,기획자,"), "2번째 줄: 학번이 비어 있습니다")
        self.assert_error(with_header("1, ,기획자,"), "2번째 줄: 이름이 비어 있습니다")
        self.assert_error(with_header("1,가, ,"), "2번째 줄: 주 역할이 비어 있습니다")

    def test_unknown_sub_role(self):
        self.assert_error(
            with_header("1,가,기획자,테스터;디자이너"), "2번째 줄: 모르는 역할 '디자이너'"
        )

    def test_blank_rows_are_skipped_but_counted(self):
        data = with_header("", "1,가,기획자,", " , , , ", "2,나,디자이너,")
        self.assert_error(data, "5번째 줄: 모르는 역할 '디자이너'")

    def test_line_numbers_count_crlf_files_too(self):
        data = with_header("1,가,기획자,", "1,나,테스터,", eol="\r\n")
        self.assert_error(data, "3번째 줄: 학번 1이 겹칩니다")

    def test_check_order_within_a_row(self):
        # 열 개수 > 학번 비어 있음 > admin > 중복 > 이름 비어 있음 > 주 역할 > 보조 역할
        self.assert_error(with_header(",가,기획자"), "2번째 줄: 열이 4개가 아닙니다")
        self.assert_error(with_header(",,,x"), "2번째 줄: 학번이 비어 있습니다")
        self.assert_error(with_header("admin,,,x"), "2번째 줄: admin은 학번으로 쓸 수 없습니다")
        self.assert_error(
            with_header("1,가,기획자,", "1,,,x"), "3번째 줄: 학번 1이 겹칩니다"
        )
        self.assert_error(with_header("1,,,x"), "2번째 줄: 이름이 비어 있습니다")
        self.assert_error(with_header("1,가,,x"), "2번째 줄: 주 역할이 비어 있습니다")
        self.assert_error(with_header("1,가,디자이너,x"), "2번째 줄: 모르는 역할 '디자이너'")
        self.assert_error(with_header("1,가,기획자,x"), "2번째 줄: 모르는 역할 'x'")

    def test_first_error_wins_across_rows(self):
        data = with_header("1,가,디자이너,", "1,나,테스터,")
        self.assert_error(data, "2번째 줄: 모르는 역할 '디자이너'")


if __name__ == "__main__":
    unittest.main()
