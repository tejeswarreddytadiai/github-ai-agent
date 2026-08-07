datasets = ["zgrps3rd", "zedps1rd", "z_sv_utl_stag_p_p"]

staging_tables_partitioned = [
 {
       table_id  = "zgrt415_ccbsrdm",
       dataset_id  = "zgrps3rd",
       schema_file  = "schema_files/zgrps3rd.zgrt415_ccbsrdm.json",
       description = "The COLLECTION SCORING OUTPUT table captures the processing information and calculated variables that are output from collections scoring."
       require_partition_filter = false,
       cluster_cols             = ["alt_acct_cntl1_nb","alt_acct_cntl2_nb","alt_acct_cntl3_nb","alt_acct_cntl4_nb"]
       exp_partition            = null,
       table_constraints = { primary_key_columns = ["alt_acct_cntl1_nb","alt_acct_cntl2_nb","alt_acct_cntl3_nb","alt_acct_cntl4_nb","rcvbl_acct_nb","la_acs_file_dt"] }
 }
]
