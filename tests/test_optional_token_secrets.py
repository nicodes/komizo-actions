import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('optional_tokens', Path(__file__).parents[1] / 'optional-token-secrets/select.py')
tokens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tokens)


class OptionalTokens(unittest.TestCase):
    def test_absent_values_are_omitted_and_trimmed_supplied_values_are_preserved(self):
        self.assertEqual(tokens.selected({'UNRELATED': 'private', 'KOMIZO_OPTIONAL_SECRET_EMPTY': '  '}), {})
        self.assertEqual(tokens.selected({'KOMIZO_OPTIONAL_SECRET_REGISTRY_TOKEN': ' fixture-value \n'}),
                         {'KOMIZO_SECRET_REGISTRY_TOKEN': 'fixture-value'})

    def test_invalid_token_or_name_cannot_write_an_environment_command(self):
        for value in ['has whitespace', 'line\nbreak', 'nul\0value', 'x' * 513]:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                tokens.selected({'KOMIZO_OPTIONAL_SECRET_TOKEN': value})
        with self.assertRaises(ValueError):
            tokens.selected({'KOMIZO_OPTIONAL_SECRET_lowercase': 'fixture-value'})


if __name__ == '__main__':
    unittest.main()
