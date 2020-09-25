import sys
import yaml

from sys import stderr
from collections import defaultdict
from typing import Iterator
from itertools import islice, chain
from pysam.libcbcf import VariantFile, VariantHeader, VariantRecord

from ..common import (
    check_expression,
    get_annotation_keys,
    split_annotation_entry,
)
from ..errors import VembraneError
from ..representations import Environment
from .. import __version__


def add_subcommmand(subparsers):
    parser = subparsers.add_parser("filter")
    parser.add_argument(
        "expression",
        type=check_expression,
        help="Filter variants and annotations. If this removes all annotations, "
        "the variant is removed as well.",
    )
    parser.add_argument(
        "vcf", help="The file containing the variants.", nargs="?", default="-"
    )
    parser.add_argument(
        "--output",
        "-o",
        default="-",
        help="Output file, if not specified, output is written to STDOUT.",
    )
    parser.add_argument(
        "--output-fmt",
        "-O",
        default="vcf",
        choices=["vcf", "bcf", "uncompressed-bcf"],
        help="Output format.",
    )
    parser.add_argument(
        "--annotation-key",
        "-k",
        metavar="FIELDNAME",
        default="ANN",
        help="The INFO key for the annotation field.",
    )
    parser.add_argument(
        "--statistics",
        "-s",
        metavar="FILE",
        default=None,
        help="Write statistics to this file.",
    )
    parser.add_argument(
        "--keep-unmatched",
        default=False,
        action="store_true",
        help="Keep all annotations of a variant if at least one of them passes "
        "the expression.",
    )
    parser.add_argument(
        "--preserve-order",
        default=False,
        action="store_true",
        help="Make sure that the variant order of the output matches that of the input. \
              For performance reasons only make use of this option, if the input might contain breakends (BNDs) \
              as the order of all other variants are preserved anyway.",
    )
    # parser.add_argument(
    #     "--events",
    #     default=False,
    #     action="store_true",
    #     help="The variant file contains breakend events. This requires two passes, so streaming is not possible.",
    # )


def test_and_update_record(env, idx, record, ann_key, keep_unmatched):
    env.update_from_record(idx, record)
    if env.expression_annotations():
        # if the expression contains a reference to the ANN field
        # get all annotations from the record.info field
        # (or supply an empty ANN value if the record has no ANN field)
        try:
            annotations = record.info[ann_key]
        except KeyError:
            annotations = [""]
        #  … and only keep the annotations where the expression evaluates to true
        filtered_annotations = [
            annotation for annotation in annotations if env.evaluate(annotation)
        ] # TODO: if keep_unmatched, that could be "any(iterator)" instead of list

        if not keep_unmatched and (len(annotations) != len(filtered_annotations)):
            # update annotations if they have actually been filtered
            record.info[ann_key] = filtered_annotations

        return record, len(filtered_annotations) > 0
    else:
        # otherwise, the annotations are irrelevant w.r.t. the expression,
        # so we can omit them
        return record, env.evaluate()



def filter_vcf(
    vcf: VariantFile,
    expression: str,
    ann_key: str,
    keep_unmatched: bool = False,
    preserve_order: bool = False,
) -> Iterator[VariantRecord]:

    env = Environment(expression, ann_key, vcf.header)

    events = set()
    info_keys = set(vcf.header.info.keys())

    record: VariantRecord
    for idx, record in enumerate(vcf):
        record, passed = test_and_update_record(env, idx, record, ann_key, keep_unmatched)
        if passed:
            is_bnd = "SVTYPE" in info_keys and record.info.get("SVTYPE", None) == "BND"
            if is_bnd:
                event = record.info.get("EVENT", None)
                events.add(event)
            elif not preserve_order:
                # if preserver order, we will output everything in the second pass *
                yield record


    if len(events) > 0:
        # perform a second pass
        vcf.reset()
        for idx, record in enumerate(vcf):
            is_bnd = "SVTYPE" in info_keys and record.info.get("SVTYPE", None) == "BND"
            event = record.info.get("EVENT", None)

            if is_bnd:
                if event not in events:
                     # only bnds with valid event
                    continue
            else:
                if not preserve_order:
                    # if preserver order, we will output everything in the second pass *
                    continue
            record, _ = test_and_update_record(env, idx, record, ann_key, keep_unmatched)
            yield record


def statistics(
    records: Iterator[VariantRecord], vcf: VariantFile, filename: str, ann_key: str
) -> Iterator[VariantRecord]:
    annotation_keys = get_annotation_keys(vcf.header, ann_key)
    counter = defaultdict(lambda: defaultdict(lambda: 0))
    for record in records:
        for annotation in record.info[ann_key]:
            for key, raw_value in zip(
                annotation_keys, split_annotation_entry(annotation)
            ):
                value = raw_value.strip()
                if value:
                    counter[key][value] += 1
        yield record

    # reduce dicts with many items, to just one counter
    for key, subdict in counter.items():
        if len(subdict) > 10:
            counter[key] = f"#{len(subdict)}"

    yaml.add_representer(defaultdict, yaml.representer.Representer.represent_dict)
    with open(filename, "w") as out:
        yaml.dump(dict(counter), out)


def execute(args):
    with VariantFile(args.vcf) as vcf:
        header: VariantHeader = vcf.header
        header.add_meta("vembraneVersion", __version__)
        header.add_meta(
            "vembraneCmd",
            "vembrane "
            + " ".join(
                "'" + arg.replace("'", '"') + '"' if " " in arg else arg
                for arg in sys.argv[1:]
            ),
        )

        records = filter_vcf(
            vcf,
            args.expression,
            args.annotation_key,
            keep_unmatched=args.keep_unmatched,
            preserve_order=args.preserve_order,
        )

        try:
            first_record = list(islice(records, 1))
        except VembraneError as ve:
            print(ve, file=stderr)
            exit(1)

        records = chain(first_record, records)

        if args.statistics is not None:
            records = statistics(records, vcf, args.statistics, args.annotation_key)

        fmt = {"vcf":"", "bcf":"b", "uncompressed-bcf": "u"}[args.output_fmt]
        with VariantFile(args.output,
            f"w{fmt}",
            header=header,
        ) as out:
            try:
                for record in records:
                    out.write(record)

            except VembraneError as ve:
                print(ve, file=stderr)
                exit(1)
