"""
CodePulse AST parsing subsystem.
"""

from codepulse.parser.base import LanguageParser
from codepulse.parser.factory import get_parser_for_file, get_parser_for_language

__all__ = ["LanguageParser", "get_parser_for_file", "get_parser_for_language"]
