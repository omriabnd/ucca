import os
import random
import time
from itertools import groupby
from random import shuffle
from xml.etree.ElementTree import ParseError

from nltk import pos_tag

from parsing.action import Action
from parsing.averaged_perceptron import AveragedPerceptron
from parsing.config import Config
from parsing.features import FeatureExtractor
from parsing.oracle import Oracle
from parsing.state import State
from ucca import core, layer0, layer1, convert, ioutil, diffutil, evaluation


class ParserException(Exception):
    pass


class Parser(object):

    """
    Main class to implement transition-based UCCA parser
    """
    def __init__(self, model_file=None):
        self.state = None  # State object created at each parse
        self.oracle = None  # Oracle object created at each parse
        self.scores = None  # dict of action IDs -> model scores at each action
        self.action_count = 0
        self.correct_count = 0
        self.total_actions = 0
        self.total_correct = 0

        self.model = AveragedPerceptron(len(Action.get_all_actions()),
                                        min_update=Config().min_update)
        self.model_file = model_file
        self.feature_extractor = FeatureExtractor()

        self.learning_rate = Config().learning_rate
        self.decay_factor = Config().decay_factor

    def train(self, passages, dev=None, iterations=1):
        """
        Train parser on given passages
        :param passages: iterable of Passage objects to train on
        :param dev: iterable of Passage objects to tune on
        :param iterations: number of iterations to perform
        :return: trained model
        """
        if not passages:
            if self.model_file is not None:  # Nothing to train on; pre-trained model given
                self.model.load(self.model_file)
            return self.model

        best_score = 0
        best_model = None
        save_model = True
        for iteration in range(iterations):
            print("Training iteration %d of %d: " % (iteration + 1, iterations))
            passages = [(passage, passage_id) for _, passage, passage_id in
                        self.parse(passages, mode="train")]
            self.learning_rate *= self.decay_factor
            shuffle(passages)
            if dev:
                print("Evaluating on dev passages")
                dev, scores = zip(*[((passage, passage_id),
                                     evaluation.evaluate(predicted_passage, passage,
                                                         verbose=False, units=False, errors=False))
                                    for predicted_passage, passage, passage_id in
                                    self.parse(dev, mode="dev")])
                score = evaluation.Scores.aggregate(scores).average_f1()
                print("Average F1 score on dev: %.3f" % score)
                if score >= best_score:
                    print("Better than previous best score (%.3f)" % best_score)
                    best_score = score
                    save_model = True
                else:
                    print("Not better than previous best score (%.3f)" % best_score)
                    save_model = False

            if save_model:
                best_model = self.model.average()
                if self.model_file is not None:
                    best_model.save(self.model_file)

            print()
        print("Trained %d iterations" % iterations)

        self.model = best_model
        return self.model

    def parse(self, passages, mode="test"):
        """
        Parse given passages
        :param passages: iterable of pairs of (passage, passage ID), where passage may be:
                         either Passage object, or list of lists of tokens
        :param mode: "train", "test" or "dev".
                     If "train", use oracle to train on given passages.
                     Otherwise, just parse with classifier.
        :return: generator of triplets of (parsed passage, original passage, passage ID)
        """
        train = (mode == "train")
        passage_word = "sentence" if Config().sentences else \
                       "paragraph" if Config().paragraphs else \
                       "passage"
        assert train or mode in ("test", "dev"), "Invalid parse mode: %s" % mode
        self.total_actions = 0
        self.total_correct = 0
        total_duration = 0
        total_tokens = 0
        num_passages = 0
        for passage, passage_id in passages:
            print("%s %-7s" % (passage_word, passage_id), end=Config().line_end, flush=True)
            started = time.time()
            self.action_count = 0
            self.correct_count = 0
            assert not train or isinstance(passage, core.Passage), "Cannot train on unannotated passage"
            self.state = State(passage, passage_id, callback=self.pos_tag)
            history = set()
            self.oracle = Oracle(passage) if isinstance(passage, core.Passage) else None
            failed = False
            try:
                self.parse_passage(history, train)  # This is where the actual parsing takes place
            except ParserException as e:
                if train:
                    raise
                Config().log("%s %s: %s" % (passage_word, passage_id, e))
                print("failed")
                failed = True
            predicted_passage = passage
            if not train or Config().verify:
                predicted_passage = self.state.create_passage(assert_proper=Config().verify)
            duration = time.time() - started
            total_duration += duration
            if not failed:
                if self.oracle:  # passage is a Passage object, and we have an oracle to verify by
                    if Config().verify:
                        self.verify_passage(passage, predicted_passage, train)
                    print("accuracy: %.3f (%d/%d)" %
                          (self.correct_count/self.action_count, self.correct_count, self.action_count)
                          if self.action_count else "No actions done", end=Config().line_end)
                num_tokens = len(passage.layer(layer0.LAYER_ID).all) if self.oracle else sum(map(len, passage))
                total_tokens += num_tokens
                print("time: %0.3fs (%d tokens/second)" % (duration, num_tokens / duration),
                      end=Config().line_end + "\n", flush=True)
            self.total_correct += self.correct_count
            self.total_actions += self.action_count
            num_passages += 1
            yield predicted_passage, passage, passage_id

        if num_passages > 1:
            print("Parsed %d %ss" % (num_passages, passage_word))
            if self.oracle and self.total_actions:
                print("Overall %s accuracy: %.3f (%d/%d)" %
                      (mode,
                       self.total_correct / self.total_actions, self.total_correct, self.total_actions))
            print("Total time: %.3fs (average time/%s: %.3fs, average tokens/second: %d)" % (
                total_duration, passage_word, total_duration / num_passages,
                total_tokens / total_duration), flush=True)

    def parse_passage(self, history=None, train=False):
        """
        Internal method to parse a single passage
        :param history: set of hashed states in the parser's history, if loop checking is enabled
        :param train: use oracle to train on given passages, or just parse with classifier?
        """
        if Config().verbose:
            print("  initial state: %s" % self.state)
        while True:
            if Config().check_loops and history is not None:
                self.check_loop(history, train)

            true_actions = []
            if self.oracle is not None:
                try:
                    true_actions = self.oracle.get_actions(self.state)
                except (AttributeError, AssertionError) as e:
                    if train:
                        raise ParserException("Error in oracle during training") from e

            features = self.feature_extractor.extract_features(self.state)
            predicted_action = self.predict_action(features, true_actions)
            action = predicted_action
            if not true_actions:
                true_actions = "?"
            elif predicted_action in true_actions:
                self.correct_count += 1
            elif train:
                best_true_action_id = max([true_action.id for true_action in true_actions],
                                          key=self.scores.get) if len(true_actions) > 1 \
                    else true_actions[0].id
                rate = self.learning_rate
                if Action.by_id(best_true_action_id).is_swap:
                    rate *= Config().importance
                self.model.update(features, predicted_action.id, best_true_action_id, rate)
                action = random.choice(true_actions)
            self.action_count += 1
            try:
                self.state.transition(action)
            except AssertionError as e:
                raise ParserException("Invalid transition: %s" % action) from e
            if Config().verbose:
                if self.oracle is None:
                    print("  action: %-15s %s" % (action, self.state))
                else:
                    print("  predicted: %-15s true: %-15s taken: %-15s %s" % (
                        predicted_action, "|".join(str(true_action) for true_action in true_actions),
                        action, self.state))
                for line in self.state.log:
                    print("    " + line)
            if self.state.finished:
                return  # action is FINISH

    def check_loop(self, history, train):
        """
        Check if the current state has already occurred, indicating a loop
        :param history: set of hashed states in the parser's history
        :param train: whether to print the oracle in case of an assertion error
        """
        h = hash(self.state)
        assert h not in history, "\n".join(["Transition loop", self.state.str("\n")] +
                                           [self.oracle.str("\n")] if train else ())
        history.add(h)

    def predict_action(self, features, true_actions=None):
        """
        Choose action based on classifier
        :param features: extracted feature values
        :param true_actions: from the oracle, to copy orig_node if the same action is selected
        :return: valid action with maximum probability according to classifier
        """
        self.scores = self.model.score(features)  # Returns dict of id -> score
        best_action = self.select_action(max(self.scores, key=self.scores.get), true_actions)
        if self.state.is_valid(best_action):
            return best_action
        # Usually the best action is valid, so max is enough to choose it in O(n) time
        # Otherwise, sort all the other scores to choose the best valid one in O(n lg n)
        sorted_ids = reversed(sorted(self.scores, key=self.scores.get))
        actions = (self.select_action(i, true_actions) for i in sorted_ids)
        try:
            return next(action for action in actions if self.state.is_valid(action))
        except StopIteration as e:
            raise ParserException("No valid actions available\n" +
                                  ("True actions: %s" % true_actions if true_actions
                                   else self.oracle.log if self.oracle is not None
                                   else "")) from e

    @staticmethod
    def select_action(i, true_actions):
        action = Action.by_id(i)
        try:
            return next(true_action for true_action in true_actions if action == true_action)
        except StopIteration:
            return action

    @staticmethod
    def verify_passage(passage, predicted_passage, show_diff):
        """
        Compare predicted passage to true passage and die if they differ
        :param passage: true passage
        :param predicted_passage: predicted passage to compare
        :param show_diff: if passages differ, show the difference between them?
                          Depends on predicted_passage having the original node IDs annotated
                          in the "remarks" field for each node.
        """
        assert passage.equals(predicted_passage, ignore_node=ignore_node),\
            "Failed to produce true passage" + \
            (diffutil.diff_passages(
                    passage, predicted_passage) if show_diff else "")

    @staticmethod
    def pos_tag(state):
        """
        Function to pass to State to POS tag the tokens when created
        :param state: State object to modify
        """
        tokens = [token for tokens in state.tokens for token in tokens]
        tokens, tags = zip(*pos_tag(tokens))
        if Config().verbose:
            print(" ".join("%s/%s" % (token, tag) for (token, tag) in zip(tokens, tags)))
        for node, tag in zip(state.nodes, tags):
            node.pos_tag = tag

    @staticmethod
    def read_passage(passage):
        """
        Read a passage given in any format
        :param passage: either a core.Passage, a file, or a list of list of strings (paragraphs, words)
        :return: a core.Passage and its ID if given a Passage or file, or else the given list of lists
        """
        if isinstance(passage, core.Passage):
            passage_id = passage.ID
        elif os.path.exists(passage):  # a file
            try:
                passage = ioutil.file2passage(passage)  # XML or binary format
                passage_id = passage.ID
            except (IOError, ParseError):
                passage_id, ext = os.path.splitext(os.path.basename(passage))
                converter = convert.CONVERTERS.get(ext.lstrip("."))
                with open(passage) as f:
                    if converter is None:  # Simple text file
                        lines = (line.strip() for line in f.readlines())
                        passage = [[token for line in group for token in line.split()]
                                   for is_sep, group in groupby(lines, lambda x: not x)
                                   if not is_sep]
                    else:  # Known extension, convert to passage
                        converter, _ = converter
                        passage = next(converter(f, passage_id))
        else:
            raise IOError("File not found: %s" % passage)
        return passage, passage_id

if Config().no_linkage:
    def ignore_node(node):
        return node.tag == layer1.NodeTags.Linkage
else:
    ignore_node = None


def read_passages(files):
    for file in files:
        passage, i = Parser.read_passage(file)
        if Config().split:
            segments = convert.split2segments(passage, is_sentences=Config().sentences,
                                              remarks=True)
            for j, segment in enumerate(segments):
                yield (segment, "%s_%d" % (i, j))
        else:
            yield (passage, i)


def read_files_and_dirs(files_and_dirs):
    """
    :param files_and_dirs: iterable of files and/or directories to look in
    :return: generator of passages from all files given,
             plus any files directly under any directory given
    """
    files = list(files_and_dirs)
    files += [os.path.join(d, f) for d in files if os.path.isdir(d) for f in os.listdir(d)]
    files = [f for f in files if not os.path.isdir(f)]
    return read_passages(files) if files else ()


def write_passage(passage, outdir, prefix, binary, verbose):
    suffix = ".pickle" if binary else ".xml"
    outfile = outdir + os.path.sep + prefix + passage.ID + suffix
    if verbose:
        print("Writing passage '%s'..." % outfile)
    ioutil.passage2file(passage, outfile, binary=binary)


def train_test(train_passages, dev_passages, test_passages, args):
    scores = None
    p = Parser(args.model)
    p.train(train_passages, dev=dev_passages, iterations=args.iterations)
    if test_passages:
        if args.train:
            print("Evaluating on test passages")
        passage_scores = []
        for guessed_passage, ref_passage, _ in p.parse(test_passages):
            if isinstance(ref_passage, core.Passage):
                passage_scores.append(evaluation.evaluate(guessed_passage, ref_passage,
                                                          verbose=args.verbose and guessed_passage is not None))
            if guessed_passage is not None:
                write_passage(guessed_passage,
                              args.outdir, args.prefix, args.binary, args.verbose)
        if passage_scores:
            scores = evaluation.Scores.aggregate(passage_scores)
            print("\nAverage F1 score on test: %.3f" % scores.average_f1())
            print("Aggregated scores:")
            scores.print()
    return scores


def main():
    args = Config().args
    print("Running parser with %s" % Config())
    if args.folds is not None:
        k = args.folds
        fold_scores = []
        all_passages = list(read_files_and_dirs(args.passages))
        assert len(all_passages) >= k,\
            "%d folds are not possible with only %d passages" % (k, len(all_passages))
        shuffle(all_passages)
        folds = [all_passages[i::k] for i in range(k)]
        for i in range(k):
            print("Fold %d of %d:" % (i + 1, k))
            dev_passages = folds[i]
            test_passages = folds[(i+1) % k]
            train_passages = [passage for fold in folds
                              if fold is not dev_passages and fold is not test_passages
                              for passage in fold]
            fold_scores.append(train_test(train_passages, dev_passages, test_passages, args))
        scores = evaluation.Scores.aggregate(fold_scores)
        print("Average F1 score for each fold: " + ", ".join(
            "%.3f" % s.average_f1() for s in fold_scores))
        print("Aggregated scores across folds:\n")
        scores.print()
    else:  # Simple train/dev/test by given arguments
        train_passages, dev_passages, test_passages = [read_files_and_dirs(arg) for arg in
                                                       (args.train, args.dev, args.passages)]
        scores = train_test(train_passages, dev_passages, test_passages, args)
    return scores


if __name__ == "__main__":
    main()
    Config().close()