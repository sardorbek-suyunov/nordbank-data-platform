# scripts

Operational and developer scripts invoked by the Makefile, by CI and by hand.

Standing rule for this project: a Make recipe longer than three lines delegates to a script
here, so that the behaviour can be read, tested and run without make.

Scripts target Python 3.11 and the standard library unless the dependency they need is
already in the project.
