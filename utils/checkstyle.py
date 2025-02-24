#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2018, Google Inc.
#
# Author: Laurent Pinchart <laurent.pinchart@ideasonboard.com>
#
# checkstyle.py - A patch style checker script based on clang-format
#
# TODO:
#
# - Support other formatting tools and checkers (cppcheck, cpplint, kwstyle, ...)
# - Split large hunks to minimize context noise
# - Improve style issues counting
#

import argparse
import difflib
import fnmatch
import os.path
import re
import shutil
import subprocess
import sys

dependencies = {
    'clang-format': True,
    'git': True,
}

# ------------------------------------------------------------------------------
# Colour terminal handling
#

class Colours:
    Default = 0
    Black = 0
    Red = 31
    Green = 32
    Yellow = 33
    Blue = 34
    Magenta = 35
    Cyan = 36
    LightGrey = 37
    DarkGrey = 90
    LightRed = 91
    LightGreen = 92
    Lightyellow = 93
    LightBlue = 94
    LightMagenta = 95
    LightCyan = 96
    White = 97

    @staticmethod
    def fg(colour):
        if sys.stdout.isatty():
            return '\033[%um' % colour
        else:
            return ''

    @staticmethod
    def bg(colour):
        if sys.stdout.isatty():
            return '\033[%um' % (colour + 10)
        else:
            return ''

    @staticmethod
    def reset():
        if sys.stdout.isatty():
            return '\033[0m'
        else:
            return ''


# ------------------------------------------------------------------------------
# Diff parsing, handling and printing
#

class DiffHunkSide(object):
    """A side of a diff hunk, recording line numbers"""
    def __init__(self, start):
        self.start = start
        self.touched = []
        self.untouched = []

    def __len__(self):
        return len(self.touched) + len(self.untouched)


class DiffHunk(object):
    diff_header_regex = re.compile(r'@@ -([0-9]+),?([0-9]+)? \+([0-9]+),?([0-9]+)? @@')

    def __init__(self, line):
        match = DiffHunk.diff_header_regex.match(line)
        if not match:
            raise RuntimeError("Malformed diff hunk header '%s'" % line)

        self.__from_line = int(match.group(1))
        self.__to_line = int(match.group(3))
        self.__from = DiffHunkSide(self.__from_line)
        self.__to = DiffHunkSide(self.__to_line)

        self.lines = []

    def __repr__(self):
        s = '%s@@ -%u,%u +%u,%u @@\n' % \
                (Colours.fg(Colours.Cyan),
                 self.__from.start, len(self.__from),
                 self.__to.start, len(self.__to))

        for line in self.lines:
            if line[0] == '-':
                s += Colours.fg(Colours.Red)
            elif line[0] == '+':
                s += Colours.fg(Colours.Green)

            if line[0] == '-':
                spaces = 0
                for i in range(len(line)):
                    if line[-i-1].isspace():
                        spaces += 1
                    else:
                        break
                spaces = len(line) - spaces
                line = line[0:spaces] + Colours.bg(Colours.Red) + line[spaces:]

            s += line
            s += Colours.reset()
            s += '\n'

        return s[:-1]

    def append(self, line):
        if line[0] == ' ':
            self.__from.untouched.append(self.__from_line)
            self.__from_line += 1
            self.__to.untouched.append(self.__to_line)
            self.__to_line += 1
        elif line[0] == '-':
            self.__from.touched.append(self.__from_line)
            self.__from_line += 1
        elif line[0] == '+':
            self.__to.touched.append(self.__to_line)
            self.__to_line += 1

        self.lines.append(line.rstrip('\n'))

    def intersects(self, lines):
        for line in lines:
            if line in self.__from.touched:
                return True
        return False

    def side(self, side):
        if side == 'from':
            return self.__from
        else:
            return self.__to


def parse_diff(diff):
    hunks = []
    hunk = None
    for line in diff:
        if line.startswith('@@'):
            if hunk:
                hunks.append(hunk)
            hunk = DiffHunk(line)

        elif hunk is not None:
            hunk.append(line)

    if hunk:
        hunks.append(hunk)

    return hunks


# ------------------------------------------------------------------------------
# Commit, Staged Changes & Amendments
#

class CommitFile:
    def __init__(self, name):
        info = name.split()
        self.__status = info[0][0]

        # For renamed files, store the new name
        if self.__status == 'R':
            self.__filename = info[2]
        else:
            self.__filename = info[1]

    def __repr__(self):
        return f'{self.__status} {self.__filename}'

    @property
    def filename(self):
        return self.__filename

    @property
    def status(self):
        return self.__status


class Commit:
    def __init__(self, commit):
        self.commit = commit
        self._parse()

    def _parse_trailers(self, lines):
        self._trailers = []
        for index in range(1, len(lines)):
            line = lines[index]
            if not line:
                break

            self._trailers.append(line)

        return index

    def _parse(self):
        # Get the commit title and list of files.
        ret = subprocess.run(['git', 'show', '--format=%s%n%(trailers:only,unfold)', '--name-status',
                              self.commit],
                             stdout=subprocess.PIPE).stdout.decode('utf-8')
        lines = ret.splitlines()

        self._title = lines[0]

        index = self._parse_trailers(lines)
        self._files = [CommitFile(f) for f in lines[index:] if f]

    def files(self, filter='AMR'):
        return [f.filename for f in self._files if f.status in filter]

    @property
    def title(self):
        return self._title

    @property
    def trailers(self):
        return self._trailers

    def get_diff(self, top_level, filename):
        diff = subprocess.run(['git', 'diff', '%s~..%s' % (self.commit, self.commit),
                               '--', '%s/%s' % (top_level, filename)],
                              stdout=subprocess.PIPE).stdout.decode('utf-8')
        return parse_diff(diff.splitlines(True))

    def get_file(self, filename):
        return subprocess.run(['git', 'show', '%s:%s' % (self.commit, filename)],
                              stdout=subprocess.PIPE).stdout.decode('utf-8')


class StagedChanges(Commit):
    def __init__(self):
        Commit.__init__(self, '')

        # There are no trailers to parse on a Staged Change.
        self._trailers = []

    def _parse(self):
        ret = subprocess.run(['git', 'diff', '--staged', '--name-status'],
                             stdout=subprocess.PIPE).stdout.decode('utf-8')
        self._title = 'Staged changes'
        self._files = [CommitFile(f) for f in ret.splitlines()]

    def get_diff(self, top_level, filename):
        diff = subprocess.run(['git', 'diff', '--staged', '--',
                               '%s/%s' % (top_level, filename)],
                              stdout=subprocess.PIPE).stdout.decode('utf-8')
        return parse_diff(diff.splitlines(True))


class Amendment(Commit):
    def __init__(self):
        Commit.__init__(self, '')

    def _parse(self):
        # Create a title using HEAD commit
        ret = subprocess.run(['git', 'show', '--pretty=oneline', '--no-patch'],
                             stdout=subprocess.PIPE).stdout.decode('utf-8')
        self._title = 'Amendment of ' + ret.strip()
        # Extract the list of modified files
        ret = subprocess.run(['git', 'diff', '--staged', '--name-status', 'HEAD~'],
                             stdout=subprocess.PIPE).stdout.decode('utf-8')
        self._files = [CommitFile(f) for f in ret.splitlines()]

        # Parse trailers from the existing commit only.
        ret = subprocess.run(['git', 'show', '--format=%n%(trailers:only,unfold)',
                             '--no-patch'],
                             stdout=subprocess.PIPE).stdout.decode('utf-8')
        self._parse_trailers(ret.splitlines())

    def get_diff(self, top_level, filename):
        diff = subprocess.run(['git', 'diff', '--staged', 'HEAD~', '--',
                               '%s/%s' % (top_level, filename)],
                              stdout=subprocess.PIPE).stdout.decode('utf-8')
        return parse_diff(diff.splitlines(True))


# ------------------------------------------------------------------------------
# Helpers
#

class ClassRegistry(type):
    def __new__(cls, clsname, bases, attrs):
        newclass = super().__new__(cls, clsname, bases, attrs)
        if bases:
            bases[0].subclasses.append(newclass)
            bases[0].subclasses.sort(key=lambda x: getattr(x, 'priority', 0),
                                     reverse=True)
        return newclass


# ------------------------------------------------------------------------------
# Commit Checkers
#

class CommitChecker(metaclass=ClassRegistry):
    subclasses = []

    def __init__(self):
        pass

    #
    # Class methods
    #
    @classmethod
    def checkers(cls, names):
        for checker in cls.subclasses:
            if names and checker.__name__ not in names:
                continue
            yield checker


class CommitIssue(object):
    def __init__(self, msg):
        self.msg = msg


class HeaderAddChecker(CommitChecker):
    @classmethod
    def check(cls, commit, top_level):
        issues = []

        meson_files = [f for f in commit.files()
                       if os.path.basename(f) == 'meson.build']

        for filename in commit.files('AR'):
            if not filename.startswith('include/libcamera/') or \
               not filename.endswith('.h'):
                continue

            meson = os.path.dirname(filename) + '/meson.build'
            header = os.path.basename(filename)

            issue = CommitIssue('Header %s added without corresponding update to %s' %
                                (filename, meson))

            if meson not in meson_files:
                issues.append(issue)
                continue

            diff = commit.get_diff(top_level, meson)
            found = False

            for hunk in diff:
                for line in hunk.lines:
                    if line[0] != '+':
                        continue

                    if line.find("'%s'" % header) != -1:
                        found = True
                        break

                if found:
                    break

            if not found:
                issues.append(issue)

        return issues


class TitleChecker(CommitChecker):
    prefix_regex = re.compile(r'^([a-zA-Z0-9_.-]+: )+')
    release_regex = re.compile(r'libcamera v[0-9]+\.[0-9]+\.[0-9]+')

    @classmethod
    def check(cls, commit, top_level):
        title = commit.title

        # Skip the check when validating staged changes (as done through a
        # pre-commit hook) as there is no title to check in that case.
        if isinstance(commit, StagedChanges):
            return []

        # Ignore release commits, they don't need a prefix.
        if TitleChecker.release_regex.fullmatch(title):
            return []

        prefix_pos = title.find(': ')
        if prefix_pos != -1 and prefix_pos != len(title) - 2:
            return []

        # Find prefix candidates by searching the git history
        msgs = subprocess.run(['git', 'log', '--no-decorate', '--oneline', '-n100', '--'] + commit.files(),
                              stdout=subprocess.PIPE).stdout.decode('utf-8')
        prefixes = {}
        prefixes_count = 0
        for msg in msgs.splitlines():
            prefix = TitleChecker.prefix_regex.match(msg)
            if not prefix:
                continue

            prefix = prefix.group(0)
            if prefix in prefixes:
                prefixes[prefix] += 1
            else:
                prefixes[prefix] = 1

            prefixes_count += 1

        if not prefixes:
            return [CommitIssue('Commit title is missing prefix')]

        # Sort the candidates by number of occurrences and pick the best ones.
        # When multiple prefixes are possible without a clear winner, we want to
        # display the most common options to the user, but without the most
        # unlikely options to avoid too long messages. As a heuristic, select
        # enough candidates to cover at least 2/3 of the possible prefixes, but
        # never more than 4 candidates.
        prefixes = list(prefixes.items())
        prefixes.sort(key=lambda x: x[1], reverse=True)

        candidates = []
        candidates_count = 0
        for prefix in prefixes:
            candidates.append(f"`{prefix[0]}'")
            candidates_count += prefix[1]
            if candidates_count >= prefixes_count * 2 / 3 or \
               len(candidates) == 4:
                break

        candidates = candidates[:-2] + [' or '.join(candidates[-2:])]
        candidates = ', '.join(candidates)

        return [CommitIssue('Commit title is missing prefix, '
                            'possible candidates are ' + candidates)]


class TrailersChecker(CommitChecker):
    commit_regex = re.compile(r'[0-9a-f]{12}[0-9a-f]* \(".*"\)')

    coverity_regex = re.compile(r'Coverity CID=.*')

    # Simple e-mail address validator regex, with an additional trailing
    # comment. The complexity of a full RFC6531 validator isn't worth the
    # additional invalid addresses it would reject.
    email_regex = re.compile(r'[^<]+ <[^@>]+@[^>]+>( # .*)?')

    link_regex = re.compile(r'https?://.*')

    @staticmethod
    def validate_reported_by(value):
        if TrailersChecker.email_regex.fullmatch(value):
            return True
        if TrailersChecker.coverity_regex.fullmatch(value):
            return True
        return False

    known_trailers = {
        'Acked-by': email_regex,
        'Bug': link_regex,
        'Fixes': commit_regex,
        'Link': link_regex,
        'Reported-by': validate_reported_by,
        'Reviewed-by': email_regex,
        'Signed-off-by': email_regex,
        'Suggested-by': email_regex,
        'Tested-by': email_regex,
    }

    trailer_regex = re.compile(r'([A-Z][a-zA-Z-]*)\s*:\s*(.*)')

    @classmethod
    def check(cls, commit, top_level):
        issues = []

        for trailer in commit.trailers:
            match = TrailersChecker.trailer_regex.fullmatch(trailer)
            if not match:
                issues.append(CommitIssue(f"Malformed commit trailer '{trailer}'"))
                continue

            key, value = match.groups()

            validator = TrailersChecker.known_trailers.get(key)
            if not validator:
                issues.append(CommitIssue(f"Invalid commit trailer key '{key}'"))
                continue

            if isinstance(validator, re.Pattern):
                valid = bool(validator.fullmatch(value))
            else:
                valid = validator(value)

            if not valid:
                issues.append(CommitIssue(f"Malformed value '{value}' for commit trailer '{key}'"))
                continue

        return issues


# ------------------------------------------------------------------------------
# Style Checkers
#

class StyleChecker(metaclass=ClassRegistry):
    subclasses = []

    def __init__(self):
        pass

    #
    # Class methods
    #
    @classmethod
    def checkers(cls, filename, names):
        for checker in cls.subclasses:
            if names and checker.__name__ not in names:
                continue
            if checker.supports(filename):
                yield checker

    @classmethod
    def supports(cls, filename):
        for pattern in cls.patterns:
            if fnmatch.fnmatch(os.path.basename(filename), pattern):
                return True
        return False

    @classmethod
    def all_patterns(cls):
        patterns = set()
        for checker in cls.subclasses:
            patterns.update(checker.patterns)

        return patterns


class StyleIssue(object):
    def __init__(self, line_number, line, msg):
        self.line_number = line_number
        self.line = line
        self.msg = msg


class IncludeChecker(StyleChecker):
    patterns = ('*.cpp', '*.h')

    headers = ('assert', 'ctype', 'errno', 'fenv', 'float', 'inttypes',
               'limits', 'locale', 'setjmp', 'signal', 'stdarg', 'stddef',
               'stdint', 'stdio', 'stdlib', 'string', 'time', 'uchar', 'wchar',
               'wctype')
    include_regex = re.compile('^#include <c([a-z]*)>')

    def __init__(self, content):
        super().__init__()
        self.__content = content

    def check(self, line_numbers):
        issues = []

        for line_number in line_numbers:
            line = self.__content[line_number - 1]
            match = IncludeChecker.include_regex.match(line)
            if not match:
                continue

            header = match.group(1)
            if header not in IncludeChecker.headers:
                continue

            issues.append(StyleIssue(line_number, line,
                                     'C compatibility header <%s.h> is preferred' % header))

        return issues


class LogCategoryChecker(StyleChecker):
    log_regex = re.compile('\\bLOG\((Debug|Info|Warning|Error|Fatal)\)')
    patterns = ('*.cpp',)

    def __init__(self, content):
        super().__init__()
        self.__content = content

    def check(self, line_numbers):
        issues = []
        for line_number in line_numbers:
            line = self.__content[line_number-1]
            if not LogCategoryChecker.log_regex.search(line):
                continue

            issues.append(StyleIssue(line_number, line, 'LOG() should use categories'))

        return issues


class MesonChecker(StyleChecker):
    patterns = ('meson.build',)

    def __init__(self, content):
        super().__init__()
        self.__content = content

    def check(self, line_numbers):
        issues = []
        for line_number in line_numbers:
            line = self.__content[line_number-1]
            if line.find('\t') != -1:
                issues.append(StyleIssue(line_number, line, 'meson.build should use spaces for indentation'))
        return issues


class Pep8Checker(StyleChecker):
    patterns = ('*.py',)
    results_regex = re.compile('stdin:([0-9]+):([0-9]+)(.*)')

    def __init__(self, content):
        super().__init__()
        self.__content = content

    def check(self, line_numbers):
        issues = []
        data = ''.join(self.__content).encode('utf-8')

        try:
            ret = subprocess.run(['pycodestyle', '--ignore=E501', '-'],
                                 input=data, stdout=subprocess.PIPE)
        except FileNotFoundError:
            issues.append(StyleIssue(0, None, 'Please install pycodestyle to validate python additions'))
            return issues

        results = ret.stdout.decode('utf-8').splitlines()
        for item in results:
            search = re.search(Pep8Checker.results_regex, item)
            line_number = int(search.group(1))
            position = int(search.group(2))
            msg = search.group(3)

            if line_number in line_numbers:
                line = self.__content[line_number - 1]
                issues.append(StyleIssue(line_number, line, msg))

        return issues


class ShellChecker(StyleChecker):
    patterns = ('*.sh',)
    results_line_regex = re.compile('In - line ([0-9]+):')

    def __init__(self, content):
        super().__init__()
        self.__content = content

    def check(self, line_numbers):
        issues = []
        data = ''.join(self.__content).encode('utf-8')

        try:
            ret = subprocess.run(['shellcheck', '-Cnever', '-'],
                                 input=data, stdout=subprocess.PIPE)
        except FileNotFoundError:
            issues.append(StyleIssue(0, None, 'Please install shellcheck to validate shell script additions'))
            return issues

        results = ret.stdout.decode('utf-8').splitlines()
        for nr, item in enumerate(results):
            search = re.search(ShellChecker.results_line_regex, item)
            if search is None:
                continue

            line_number = int(search.group(1))
            line = results[nr + 1]
            msg = results[nr + 2]

            # Determined, but not yet used
            position = msg.find('^') + 1

            if line_number in line_numbers:
                issues.append(StyleIssue(line_number, line, msg))

        return issues


# ------------------------------------------------------------------------------
# Formatters
#

class Formatter(metaclass=ClassRegistry):
    subclasses = []

    def __init__(self):
        pass

    #
    # Class methods
    #
    @classmethod
    def formatters(cls, filename, names):
        for formatter in cls.subclasses:
            if names and formatter.__name__ not in names:
                continue
            if formatter.supports(filename):
                yield formatter

    @classmethod
    def supports(cls, filename):
        for pattern in cls.patterns:
            if fnmatch.fnmatch(os.path.basename(filename), pattern):
                return True
        return False

    @classmethod
    def all_patterns(cls):
        patterns = set()
        for formatter in cls.subclasses:
            patterns.update(formatter.patterns)

        return patterns


class CLangFormatter(Formatter):
    patterns = ('*.c', '*.cpp', '*.h')
    priority = -1

    @classmethod
    def format(cls, filename, data):
        ret = subprocess.run(['clang-format', '-style=file',
                              '-assume-filename=' + filename],
                             input=data.encode('utf-8'), stdout=subprocess.PIPE)
        return ret.stdout.decode('utf-8')


class DoxygenFormatter(Formatter):
    patterns = ('*.c', '*.cpp')

    return_regex = re.compile(' +\\* +\\\\return +[a-z]')

    @classmethod
    def format(cls, filename, data):
        lines = []
        in_doxygen = False

        for line in data.split('\n'):
            if line.find('/**') != -1:
                in_doxygen = True

            if not in_doxygen:
                lines.append(line)
                continue

            line = cls.return_regex.sub(lambda m: m.group(0)[:-1] + m.group(0)[-1].upper(), line)

            if line.find('*/') != -1:
                in_doxygen = False

            lines.append(line)

        return '\n'.join(lines)


class DPointerFormatter(Formatter):
    # Ensure consistent naming of variables related to the d-pointer design
    # pattern.
    patterns = ('*.cpp', '*.h')

    # The clang formatter runs first, we can thus rely on appropriate coding
    # style.
    declare_regex = re.compile(r'^(\t*)(const )?([a-zA-Z0-9_]+) \*( ?const )?([a-zA-Z0-9_]+) = (LIBCAMERA_[DO]_PTR)\(([a-zA-Z0-9_]+)\);$')

    @classmethod
    def format(cls, filename, data):
        lines = []

        for line in data.split('\n'):
            match = cls.declare_regex.match(line)
            if match:
                indent = match.group(1) or ''
                const = match.group(2) or ''
                macro = match.group(6)
                klass = match.group(7)
                if macro == 'LIBCAMERA_D_PTR':
                    var = 'Private *const d'
                else:
                    var = f'{klass} *const o'

                line = f'{indent}{const}{var} = {macro}({klass});'

            lines.append(line)

        return '\n'.join(lines)


class IncludeOrderFormatter(Formatter):
    patterns = ('*.cpp', '*.h')

    include_regex = re.compile('^#include (["<])([^">]*)([">])')

    @classmethod
    def format(cls, filename, data):
        lines = []
        includes = []

        # Parse blocks of #include statements, and output them as a sorted list
        # when we reach a non #include statement.
        for line in data.split('\n'):
            match = IncludeOrderFormatter.include_regex.match(line)
            if match:
                # If the current line is an #include statement, add it to the
                # includes group and continue to the next line.
                open_token = match.group(1)
                file_name = match.group(2)
                close_token = match.group(3)

                # Ensure the "..." include style for internal headers and the
                # <...> style for all other libcamera headers.
                if (file_name.startswith('libcamera/internal')):
                    open_token = '"'
                    close_token = '"'
                elif (file_name.startswith('libcamera/')):
                    open_token = '<'
                    close_token = '>'

                line = f'#include {open_token}{file_name}{close_token}'
                includes.append((line, file_name))
                continue

            # The current line is not an #include statement, output the sorted
            # stashed includes first, and then the current line.
            if len(includes):
                includes.sort(key=lambda i: i[1])
                for include in includes:
                    lines.append(include[0])
                includes = []

            lines.append(line)

        # In the unlikely case the file ends with an #include statement, make
        # sure we output the stashed includes.
        if len(includes):
            includes.sort(key=lambda i: i[1])
            for include in includes:
                lines.append(include[0])
            includes = []

        return '\n'.join(lines)


class StripTrailingSpaceFormatter(Formatter):
    patterns = ('*.c', '*.cpp', '*.h', '*.py', 'meson.build')

    @classmethod
    def format(cls, filename, data):
        lines = data.split('\n')
        for i in range(len(lines)):
            lines[i] = lines[i].rstrip() + '\n'
        return ''.join(lines)


# ------------------------------------------------------------------------------
# Style checking
#

def check_file(top_level, commit, filename, checkers):
    # Extract the line numbers touched by the commit.
    commit_diff = commit.get_diff(top_level, filename)

    lines = []
    for hunk in commit_diff:
        lines.extend(hunk.side('to').touched)

    # Skip commits that don't add any line.
    if len(lines) == 0:
        return 0

    # Format the file after the commit with all formatters and compute the diff
    # between the unformatted and formatted contents.
    after = commit.get_file(filename)

    formatted = after
    for formatter in Formatter.formatters(filename, checkers):
        formatted = formatter.format(filename, formatted)

    after = after.splitlines(True)
    formatted = formatted.splitlines(True)
    diff = difflib.unified_diff(after, formatted)

    # Split the diff in hunks, recording line number ranges for each hunk, and
    # filter out hunks that are not touched by the commit.
    formatted_diff = parse_diff(diff)
    formatted_diff = [hunk for hunk in formatted_diff if hunk.intersects(lines)]

    # Check for code issues not related to formatting.
    issues = []
    for checker in StyleChecker.checkers(filename, checkers):
        checker = checker(after)
        for hunk in commit_diff:
            issues += checker.check(hunk.side('to').touched)

    # Print the detected issues.
    if len(issues) == 0 and len(formatted_diff) == 0:
        return 0

    print('%s---' % Colours.fg(Colours.Red), filename)
    print('%s+++' % Colours.fg(Colours.Green), filename)

    if len(formatted_diff):
        for hunk in formatted_diff:
            print(hunk)

    if len(issues):
        issues = sorted(issues, key=lambda i: i.line_number)
        for issue in issues:
            print('%s#%u: %s%s' % (Colours.fg(Colours.Yellow), issue.line_number,
                                   issue.msg, Colours.reset()))
            if issue.line is not None:
                print('%s+%s%s' % (Colours.fg(Colours.Yellow), issue.line.rstrip(),
                                   Colours.reset()))

    return len(formatted_diff) + len(issues)


def check_style(top_level, commit, checkers):
    title = commit.commit + ' ' + commit.title
    separator = '-' * len(title)
    print(separator)
    print(title)
    print(separator)

    issues = 0

    # Apply the commit checkers first.
    for checker in CommitChecker.checkers(checkers):
        for issue in checker.check(commit, top_level):
            print('%s%s%s' % (Colours.fg(Colours.Yellow), issue.msg, Colours.reset()))
            issues += 1

    # Filter out files we have no checker for.
    patterns = set()
    patterns.update(StyleChecker.all_patterns())
    patterns.update(Formatter.all_patterns())
    files = [f for f in commit.files() if len([p for p in patterns if fnmatch.fnmatch(os.path.basename(f), p)])]

    for f in files:
        issues += check_file(top_level, commit, f, checkers)

    if issues == 0:
        print('No issue detected')
    else:
        print('---')
        print('%u potential %s detected, please review' %
              (issues, 'issue' if issues == 1 else 'issues'))

    return issues


def extract_commits(revs):
    """Extract a list of commits on which to operate from a revision or revision
    range.
    """
    ret = subprocess.run(['git', 'rev-parse', revs], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if ret.returncode != 0:
        print(ret.stderr.decode('utf-8').splitlines()[0])
        return []

    revlist = ret.stdout.decode('utf-8').splitlines()

    # If the revlist contains more than one item, pass it to git rev-list to list
    # each commit individually.
    if len(revlist) > 1:
        ret = subprocess.run(['git', 'rev-list', *revlist], stdout=subprocess.PIPE)
        revlist = ret.stdout.decode('utf-8').splitlines()
        revlist.reverse()

    return [Commit(x) for x in revlist]


def git_top_level():
    """Get the absolute path of the git top-level directory."""
    ret = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if ret.returncode != 0:
        print(ret.stderr.decode('utf-8').splitlines()[0])
        return None

    return ret.stdout.decode('utf-8').strip()


def main(argv):

    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkers', '-c', type=str,
                        help='Specify which checkers to run as a comma-separated list. Defaults to all checkers')
    parser.add_argument('--staged', '-s', action='store_true',
                        help='Include the changes in the index. Defaults to False')
    parser.add_argument('--amend', '-a', action='store_true',
                        help='Include changes in the index and the previous patch combined. Defaults to False')
    parser.add_argument('revision_range', type=str, default=None, nargs='?',
                        help='Revision range (as defined by git rev-parse). Defaults to HEAD if not specified.')
    args = parser.parse_args(argv[1:])

    if args.checkers:
        args.checkers = args.checkers.split(',')

    # Check for required dependencies.
    for command, mandatory in dependencies.items():
        found = shutil.which(command)
        if mandatory and not found:
            print('Executable %s not found' % command)
            return 1

        dependencies[command] = found

    # Get the top level directory to pass absolute file names to git diff
    # commands, in order to support execution from subdirectories of the git
    # tree.
    top_level = git_top_level()
    if top_level is None:
            return 1

    commits = []
    if args.staged:
        commits.append(StagedChanges())
    if args.amend:
        commits.append(Amendment())

    # If none of --staged or --amend was passed
    if len(commits) == 0:
        # And no revisions were passed, then default to HEAD
        if not args.revision_range:
            args.revision_range = 'HEAD'

    if args.revision_range:
        commits += extract_commits(args.revision_range)

    issues = 0
    for commit in commits:
        issues += check_style(top_level, commit, args.checkers)
        print('')

    if issues:
        return 1
    else:
        return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
