"""Process PDB entries with specific Gene Ontology annotations."""
import tempfile
from go2pdb.go import uniprot
import logging
import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
import pandas as pd
import numpy as np
from . import pdb, blast
from .go import goa


_LOGGER = logging.getLogger(__name__)
GO_IDS = []
BLAST_SIMILARITY_CUTOFF = 0.8
BLAST_IDENTITY_CUTOFF = 0.8
CLUSTER_IDENTITY_CUTOFF = 0.9
DATA_DIR = Path("data")
GOA_PATH = DATA_DIR / Path("goa_pdb.gaf.gz")
SEARCH_OUTPUT = Path("search-output.xlsx")
BLAST_OUTPUT = Path("blast-output.xlsx")
CLUSTER_OUTPUT = Path("cluster-output.xlsx")


def build_parser() -> argparse.ArgumentParser:
    """Build an argument parser."""
    parser = argparse.ArgumentParser(
        "Find PDB entries with GO annotations and keywords.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--broken-ssl",
        help=(
            "Use this flag if your IT department installs a self-signed "
            "certificate on the firewall without telling anyone."
        ),
        action="store_false",
        dest="working_ssl",
    )
    parser.add_argument(
        "--log-level",
        help="Output verbosity",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    subparsers = parser.add_subparsers(description="sub-command help")
    search_parser = subparsers.add_parser(
        "search",
        help="Search for PDB entries.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    search_parser.add_argument("--do-search", help=argparse.SUPPRESS)
    search_parser.add_argument(
        "--search-goa",
        help="Search the GOA database (often missing entries)",
        action="store_true",
    )
    search_parser.add_argument(
        "--local-goa-path",
        help="Path to local GZIPped copy of GOA database",
        default=GOA_PATH,
    )
    search_parser.add_argument(
        "--pdb-keyword",
        help=(
            "Option keyword to search against PDB database in case you do "
            "not trust the completeness of GO annotations"
        ),
    )
    search_parser.add_argument(
        "--output-path",
        help="Path for Excel-format search output.",
        dest="search_output_path",
        default=SEARCH_OUTPUT,
    )
    search_parser.add_argument(
        "go_codes",
        help=(
            "GO codes to search (e.g., GO:0016151). Multiple codes can be "
            "provided. Search terms are combined with logical OR operation."
        ),
        nargs="+",
    )
    blast_parser = subparsers.add_parser(
        "blast",
        help="Perform all-vs-all sequence alignments.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    blast_parser.add_argument("--do-blast", help=argparse.SUPPRESS)
    blast_parser.add_argument(
        "--input-path",
        help="Path to Excel-format search output.",
        dest="blast_input_path",
        default=SEARCH_OUTPUT,
    )
    blast_parser.add_argument(
        "--output-path",
        help="Path for Excel-format BLAST output.",
        dest="blast_output_path",
        default=BLAST_OUTPUT,
    )
    blast_parser.add_argument(
        "--fasta-path",
        help="Save FASTA file to this path (rather than a temporary file).",
    )
    blast_parser.add_argument(
        "--db-dir",
        help=(
            "Save BLAST database files to this directory (rather than a "
            " temporary directory)."
        ),
        dest="blast_db_dir",
    )
    blast_parser.add_argument(
        "--raw-output",
        help="Save raw BLAST XML-format to this path, if specified.",
        dest="blast_raw_output",
        nargs=1,
    )
    blast_parser.add_argument(
        "--identity-cutoff",
        help=(
            "Cutoff for identity score (calculated via Jaccard index). "
            "If either a hit identity or similarity exceeds these cutoffs, "
            "then the hit is included in the results.  Set to zero to include "
            "everything (but beware of file sizes...)."
        ),
        default=BLAST_IDENTITY_CUTOFF,
        dest="blast_identity_cutoff",
        type=float,
    )
    blast_parser.add_argument(
        "--similarity-cutoff",
        help=(
            "Cutoff for similarity score (calculated via Jaccard index). "
            "If either a hit identity or similarity exceeds these cutoffs, "
            "then the hit is included in the results.  Set to zero to include "
            "everything (but beware of file sizes...)."
        ),
        default=BLAST_SIMILARITY_CUTOFF,
        dest="blast_similarity_cutoff",
        type=float,
    )
    cluster_parser = subparsers.add_parser(
        "cluster",
        help="Cluster BLAST results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    cluster_parser.add_argument("--do-cluster", help=argparse.SUPPRESS)
    cluster_parser.add_argument(
        "--input-path",
        help="Path to Excel-format BLAST output.",
        default=BLAST_OUTPUT,
        dest="cluster_input_path",
    )
    cluster_parser.add_argument(
        "--output-path",
        help="Path for Excel-format cluster output.",
        default=CLUSTER_OUTPUT,
        dest="cluster_output_path",
    )
    return parser


def do_blast(args):
    """Perform BLAST all-vs.-all comparison.

    :param argparse.Namespace args:  command-line arguments
    """
    _LOGGER.info(f"Reading search output from {args.blast_input_path}.")
    search_df = pd.read_excel(args.blast_input_path)
    fasta = blast.build_fasta(search_df)
    temp_dir = TemporaryDirectory()
    if args.fasta_path is not None:
        fasta_path = args.fasta_path
    else:
        fasta_path = Path(temp_dir.name) / Path("sequences.fasta")
    _LOGGER.info(f"Writing sequence data to {fasta_path}.")
    with open(fasta_path, "wt") as fasta_file:
        fasta_file.write(fasta)
    if args.blast_db_dir is not None:
        blast_dir = Path(args.blast_db_dir)
    else:
        blast_dir = Path(temp_dir.name)
    _LOGGER.info(f"Building BLAST database in {blast_dir}.")
    blast.build_db(fasta_path, blast_dir)
    print(args)
    if args.blast_raw_output is not None:
        save_output = Path(args.blast_raw_output[0])
    else:
        save_output = None
    df = blast.run_blast(
        fasta_path,
        blast_dir,
        identity_cutoff=args.blast_identity_cutoff,
        similarity_cutoff=args.blast_similarity_cutoff,
        save_output=save_output,
    )
    _LOGGER.info(f"Saving BLAST results to {args.blast_output_path}.")
    df.to_excel(Path(args.blast_output_path))


def do_search(args):
    """Perform search.

    :param argparse.Namespace args:  command-line arguments
    """
    _LOGGER.info(f"Searching UniProt for GO codes {args.go_codes}.")
    uniprot_df = uniprot.search_go(args.go_codes, ssl_verify=args.working_ssl)
    _LOGGER.info(f"Found {len(uniprot_df)} UniProt IDs.")
    _LOGGER.info("Searching for PDB IDs matching UniProt IDs.")
    pdb_mapping_df = uniprot.get_pdb_ids(
        uniprot_df["UniProt entry ID"].values, ssl_verify=args.working_ssl
    )
    _LOGGER.info(f"Found {len(pdb_mapping_df)} PDB IDs.")
    df = uniprot_df.merge(pdb_mapping_df, how="right", on="UniProt entry ID")
    if args.pdb_keyword:
        _LOGGER.info(f"Searching PDB for keyword {args.pdb_keyword}.")
        pdb_df = pdb.keyword_search(
            args.pdb_keyword, ssl_verify=args.working_ssl
        )
        _LOGGER.info(f"Found {len(pdb_df)} PDB IDs.")
        df = df.merge(pdb_df, how="outer", on="PDB ID")
        _LOGGER.info(f"Have {len(df)} entries.")
    if args.search_goa:
        _LOGGER.info("Searching GOA.")
        goa.check_fetch(args.local_goa_path)
        goa_df = goa.extract(args.local_goa_path, args.go_codes)
        split_df = goa_df["GOA DB object ID"].str.split("_", expand=True)
        goa_df["PDB ID"] = split_df[0].str.upper()
        goa_df["Chain ID"] = split_df[1]
        goa_df = goa_df.drop(["GOA DB object ID"], axis=1)
        df = df.merge(goa_df, how="outer", on="PDB ID")
        _LOGGER.info(f"Have {len(df)} entries.")
    pdb_ids = set(df["PDB ID"].values)
    _LOGGER.info("Adding PDB metadata.")
    meta_df = pdb.metadata(pdb_ids, ssl_verify=args.working_ssl)
    df = df.merge(meta_df, how="left", on="PDB ID")
    df = df[
        [
            "PDB ID",
            "PDB description",
            "PDB title",
            "PDB keyword",
            "PDB deposit date",
            "PDB method",
            "PDB resolution (A)",
            "GOA qualifiers",
            "GOA GO code",
            "GOA DB reference",
            "GOA evidence",
            "GOA additional evidence",
            "GOA taxon ID",
            "GOA annotation date",
            "GOA assigned by",
            "PDB chain ID",
            "PDB strand ID(s)",
            "PDB strand UniProt",
            "PDB strand type",
            "PDB strand sequence",
        ]
    ]
    df = df.drop_duplicates()
    _LOGGER.info(f"Have {len(df)} entries.")
    _LOGGER.info(f"Writing results to {args.search_output_path}.")
    df.to_excel(args.search_output_path, index=False)


def main(args=None):
    """Main driver.

    :param list args:  optional list of arguments for argparse (for testing)
    """
    parser = build_parser()
    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)
    logging.basicConfig(level=getattr(logging, args.log_level, "INFO"))
    _LOGGER.debug(f"Got arguments: {args}.")
    if hasattr(args, "do_search"):
        do_search(args)
    elif hasattr(args, "do_blast"):
        do_blast(args)
    elif hasattr(args, "do_cluster"):
        raise NotImplementedError("cluster")
    else:
        _LOGGER.error("No command specified.")
        parser.print_help()


if __name__ == "__main__":
    main()
