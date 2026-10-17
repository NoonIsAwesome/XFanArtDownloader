"""Make legacy Chinese-copy assertions independent of the test machine's UI."""
from unittest.mock import patch
import i18n


def traditional_chinese_system(testcase):
    original = i18n.resolve_language
    replace = patch('i18n.resolve_language',
                    lambda mode, system_locale=None: original(mode, system_locale or 'zh_TW'))
    replace.start()
    testcase.addCleanup(replace.stop)
