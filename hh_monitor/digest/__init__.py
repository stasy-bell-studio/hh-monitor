"""Digest export — temporary candidate panel until TG bot is live.

Provides:
  fetch_candidates()  — SQL query (resumes + events + lateral snapshot)
  export_xlsx()       — openpyxl workbook with 14 columns
  export_pdf()        — WeasyPrint HTML→PDF, one card per candidate
"""
