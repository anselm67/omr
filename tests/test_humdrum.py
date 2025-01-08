import unittest
from unittest.mock import Mock, call

from kern.humdrum import Handler, Parser
from kern.typing import Note, Pitch, Symbol


class EmptySpine:
    pass


class EmptyHandler(Handler[EmptySpine()]):

    def open_spine(self) -> EmptySpine:
        return EmptySpine()

    def close_spine(self, spine: EmptySpine):
        pass

    def branch_spine(self, source: EmptySpine) -> EmptySpine:
        return EmptySpine()

    def merge_spines(self, source: EmptySpine, into: EmptySpine):
        pass

    def append(self, spine: EmptySpine, token: Symbol):
        pass


class TestHumdrumParser(unittest.TestCase):

    def ok(self, text: str):
        parser = Parser.from_text(text, EmptyHandler())
        parser.parse()

    def fail(self, text: str):
        parser = Parser.from_text(text, EmptyHandler())
        with self.assertRaises(ValueError):
            parser.parse()

    def test_one_token(self, text: str, expected_token: Symbol):
        mock_handler = Mock()
        handler_instance = mock_handler.return_value
        parser = Parser.from_text(
            "**kern\n" + text + "\n",
            handler_instance
        )
        parser.parse()
        self.assertEqual(handler_instance.open_spine.call_count, 1)
        handler_instance.append.assert_has_calls([call(
            handler_instance.open_spine.return_value,
            expected_token
        )])

    def test_kerns(self):
        self.fail("")
        self.ok(
            "**kern\t**kern\n"
            "*clefF4\t*clefG2"
        )

    def test_spine_indicators(self):
        self.ok(
            "**kern\t**kern\n"
            "*-\t*"
        )

    def test_handler_called(self):
        mock_handler = Mock()
        handler_instance = mock_handler.return_value
        parser = Parser.from_text(
            "**kern\t**kern\n",
            handler_instance
        )
        parser.parse()
        self.assertEqual(handler_instance.open_spine.call_count, 2)

    def test_note_parsing(self):
        mock_handler = Mock()
        handler_instance = mock_handler.return_value
        parser = Parser.from_text(
            "**kern\n"
            "8A\n",
            handler_instance
        )
        parser.parse()
        self.assertEqual(handler_instance.open_spine.call_count, 1)
        handler_instance.append.assert_has_calls([call(
            handler_instance.open_spine.return_value, Note(
                pitch=Pitch.A,
                duration=8
            ))])

    def test_some_tokens(self):
        self.test_one_token("8A\n", Note(
            pitch=Pitch.A,
            duration=8
        ))
        self.test_one_token("8A-\n", Note(
            pitch=Pitch.A,
            duration=8,
            flats=1,
        ))
        self.test_one_token("8A##LL\n", Note(
            pitch=Pitch.A,
            duration=8,
            sharps=2,
            starts_beam=2
        ))
        self.test_one_token("8A##\n", Note(
            pitch=Pitch.A,
            duration=8,
            sharps=2,
        ))


if __name__ == '__main__':
    unittest.main()
