#!/usr/bin/env python

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup


setup(name="storm",
    version="0.9",
    description="Storm is an object-relational mapper (ORM) for Python developed at Canonical.",
    author="Gustavo Niemeyer",
    author_email="gustavo@niemeyer.net",
    url="https://storm.canonical.com/",
    packages=[
        "storm",
        "storm.databases",
    ],
)