# HiveMind paper build targets

MAIN = hivemind
ARXIV_ZIP = hivemind-arxiv.zip

# LaTeX build
.PHONY: paper clean figures arxiv

paper: figures
	pdflatex -interaction=nonstopmode $(MAIN).tex
	bibtex $(MAIN)
	pdflatex -interaction=nonstopmode $(MAIN).tex
	pdflatex -interaction=nonstopmode $(MAIN).tex

# Generate figures from benchmark data
figures:
	python3 generate_figures.py

# Package for arXiv submission
arxiv: paper
	@echo "Packaging for arXiv..."
	@rm -f $(ARXIV_ZIP)
	zip $(ARXIV_ZIP) \
		$(MAIN).tex \
		$(MAIN).bbl \
		references.bib \
		fig_failure_rates.pdf \
		fig_throughput.pdf \
		fig_ablation.pdf \
		fig_token_waste.pdf \
		fig_cost.pdf
	@echo "Created $(ARXIV_ZIP) — ready for arXiv upload"
	@echo "Contents:"
	@unzip -l $(ARXIV_ZIP)

# Clean build artifacts
clean:
	rm -f $(MAIN).aux $(MAIN).bbl $(MAIN).blg $(MAIN).log \
		$(MAIN).out $(MAIN).synctex.gz texput.log
