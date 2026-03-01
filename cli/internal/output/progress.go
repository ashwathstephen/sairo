package output

import (
	"fmt"
	"os"

	"github.com/schollz/progressbar/v3"
)

// NewProgressBar creates a progress bar for file transfers.
func NewProgressBar(total int64, description string) *progressbar.ProgressBar {
	return progressbar.NewOptions64(total,
		progressbar.OptionSetDescription(description),
		progressbar.OptionSetWriter(os.Stderr),
		progressbar.OptionShowBytes(true),
		progressbar.OptionShowCount(),
		progressbar.OptionSetWidth(30),
		progressbar.OptionThrottle(100),
		progressbar.OptionOnCompletion(func() {
			fmt.Fprint(os.Stderr, "\n")
		}),
		progressbar.OptionSetTheme(progressbar.Theme{
			Saucer:        "█",
			SaucerHead:    "█",
			SaucerPadding: "░",
			BarStart:      "",
			BarEnd:        "",
		}),
	)
}

// NewSpinner creates an indeterminate spinner for operations without known size.
func NewSpinner(description string) *progressbar.ProgressBar {
	return progressbar.NewOptions(-1,
		progressbar.OptionSetDescription(description),
		progressbar.OptionSetWriter(os.Stderr),
		progressbar.OptionSpinnerType(14),
		progressbar.OptionOnCompletion(func() {
			fmt.Fprint(os.Stderr, "\n")
		}),
	)
}
