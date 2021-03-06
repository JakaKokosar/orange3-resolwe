from os import path, walk

import sys
from setuptools import setup, find_packages

NAME = "Orange3-resolwe"
DESCRIPTION = "Add-on containing resolwe based widgets"
LONG_DESCRIPTION = open(path.join(path.dirname(__file__), 'README.md')).read()
AUTHOR = 'Jaka Kokosar'
AUTHOR_EMAIL = 'jaka.kokosar@gmail.com'
VERSION = "0.0.1"
LICENSE = 'GPL3+'

KEYWORDS = (
    # [PyPi](https://pypi.python.org) packages with keyword "orange3 add-on"
    # can be installed using the Orange Add-on Manager
    # 'orange3 add-on',
)

PACKAGES = find_packages()

requirements = ['requirements.txt']
INSTALL_REQUIRES = sorted(set(
    line.partition('#')[0].strip()
    for file in (path.join(path.dirname(__file__), file)
                 for file in requirements)
    for line in open(file)
) - {''})

ENTRY_POINTS = {
    # Entry points that marks this package as an orange add-on. If set, addon will
    # be shown in the add-ons manager even if not published on PyPi.
    'orange3.addon': (
        'resolwe = orangecontrib.resolwe',
    ),
    # Entry point used to specify packages containing widgets.
    'orange.widgets': (
        # Syntax: category name = path.to.package.containing.widgets
        # Widget category specification can be seen in
        #    orangecontrib/resolwe/widgets/__init__.py
        'Examples = orangecontrib.resolwe.widgets',
    ),

    # Register widget help
    "orange.canvas.help": (
        'html-index = orangecontrib.resolwe.widgets:WIDGET_HELP_PATH',)
}

NAMESPACE_PACKAGES = ["orangecontrib"]

TEST_SUITE = "orangecontrib.resolwe.tests.suite"

DATA_FILES = [
    # Data files that will be installed outside site-packages folder
]


def include_documentation(local_dir, install_dir):
    global DATA_FILES
    if 'bdist_wheel' in sys.argv and not path.exists(local_dir):
        print("Directory '{}' does not exist. "
              "Please build documentation before running bdist_wheel."
              .format(path.abspath(local_dir)))
        sys.exit(0)

    doc_files = []
    for dirpath, dirs, files in walk(local_dir):
        doc_files.append((dirpath.replace(local_dir, install_dir),
                          [path.join(dirpath, f) for f in files]))
    DATA_FILES.extend(doc_files)


if __name__ == '__main__':
    include_documentation('doc/build/html', 'help/orange3-resolwe')
    setup(
        name=NAME,
        version=VERSION,
        description=DESCRIPTION,
        long_description=LONG_DESCRIPTION,
        license=LICENSE,
        packages=PACKAGES,
        data_files=DATA_FILES,
        install_requires=INSTALL_REQUIRES,
        entry_points=ENTRY_POINTS,
        keywords=KEYWORDS,
        namespace_packages=NAMESPACE_PACKAGES,
        test_suite=TEST_SUITE,
        include_package_data=True,
        zip_safe=False,
    )
