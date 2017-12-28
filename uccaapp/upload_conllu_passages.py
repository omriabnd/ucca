#!/usr/bin/env python3
import argparse
import re
import sys
from glob import glob

from ucca.convert import to_json, from_text
from uccaapp.api import ServerAccessor

try:
    from simplejson.scanner import JSONDecodeError
except ImportError:
    from json.decoder import JSONDecodeError

desc = """Upload passages from CoNLL-U files"""


class ConlluPassageUploader(ServerAccessor):
    def __init__(self, user_id, **kwargs):
        super().__init__(**kwargs)
        self.set_user(user_id)
        
    def upload_passages(self, filenames, **kwargs):
        del kwargs
        for pattern in filenames:
            filenames = glob(pattern)
            if not filenames:
                raise IOError("Not found: " + pattern)
            for filename in sorted(filenames):
                with open(filename, encoding="utf-8") as f:
                    external_id = None
                    tokens = []
                    try:
                        for line in f:
                            line = line.strip()
                            m = re.match("^# sent_id = (.*)", line)
                            if m:
                                external_id = m.group(1)
                            elif line:
                                tokens.append(line.split("\t")[1])
                            else:
                                self.upload_passage(external_id, tokens)
                                external_id = None
                                tokens = []
                        if tokens:
                            self.upload_passage(external_id, tokens)
                    except (IndexError, AssertionError) as e:
                        raise ValueError(filename) from e

    def upload_passage(self, external_id, tokens):
        assert external_id, "Missing external ID for passage %s" % tokens
        assert tokens, "Empty passage %s" % external_id
        passage_out = self.create_passage(text=" ".join(tokens), external_id=external_id, type="PUBLIC",
                                          source=self.source)
        task_in = dict(type="TOKENIZATION", status="SUBMITTED", project=self.project,
                       user=self.user, passage=passage_out, manager_comment="External ID: "+external_id,
                       user_comment="", parent=None, is_demo=False, is_active=True)
        tok_task_out = self.create_tokenization_task(**task_in)
        tok_user_task_in = dict(tok_task_out)
        passage = list(from_text(tokens, tokenized=True))[0]
        tok_user_task_in.update(to_json(passage, return_dict=True, tok_task=True))
        self.submit_tokenization_task(**tok_user_task_in)
        print("Uploaded passage "+external_id+" successfully")
            
    @staticmethod
    def add_arguments(argparser):
        argparser.add_argument("filenames", nargs="+", help="filename pattern of CoNLL-U files")
        ServerAccessor.add_user_id_argument(argparser)
        ServerAccessor.add_arguments(argparser)


def main(**kwargs):
    ConlluPassageUploader(**kwargs).upload_passages(**kwargs)


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser(description=desc)
    ConlluPassageUploader.add_arguments(argument_parser)
    main(**vars(argument_parser.parse_args()))
    sys.exit(0)

