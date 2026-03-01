package output

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/fatih/color"
	"github.com/mattn/go-isatty"
)

// IsTTY returns true if stdout is a terminal.
func IsTTY() bool {
	return isatty.IsTerminal(os.Stdout.Fd()) || isatty.IsCygwinTerminal(os.Stdout.Fd())
}

// Table renders aligned columns with optional headers.
type Table struct {
	headers []string
	rows    [][]string
	widths  []int
	writer  io.Writer
}

// NewTable creates a table with the given headers.
func NewTable(headers ...string) *Table {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = len(h)
	}
	return &Table{
		headers: headers,
		widths:  widths,
		writer:  os.Stdout,
	}
}

// AddRow adds a row to the table.
func (t *Table) AddRow(cols ...string) {
	// Pad or trim to match header count
	row := make([]string, len(t.headers))
	for i := range row {
		if i < len(cols) {
			row[i] = cols[i]
		}
		if len(row[i]) > t.widths[i] {
			t.widths[i] = len(row[i])
		}
	}
	t.rows = append(t.rows, row)
}

// Render writes the table to stdout.
// In TTY mode: colorized headers with aligned columns.
// In pipe mode: tab-separated values, no headers.
func (t *Table) Render() {
	if !IsTTY() {
		t.renderPipe()
		return
	}
	t.renderTTY()
}

func (t *Table) renderTTY() {
	// Header
	headerColor := color.New(color.FgHiWhite, color.Bold)
	sepColor := color.New(color.FgHiBlack)
	for i, h := range t.headers {
		if i > 0 {
			fmt.Print("  ")
		}
		headerColor.Printf("%-*s", t.widths[i], h)
	}
	fmt.Println()

	// Separator
	for i, w := range t.widths {
		if i > 0 {
			fmt.Print("  ")
		}
		sepColor.Print(strings.Repeat("─", w))
	}
	fmt.Println()

	// Rows
	for _, row := range t.rows {
		for i, col := range row {
			if i > 0 {
				fmt.Print("  ")
			}
			fmt.Printf("%-*s", t.widths[i], col)
		}
		fmt.Println()
	}
}

func (t *Table) renderPipe() {
	for _, row := range t.rows {
		fmt.Println(strings.Join(row, "\t"))
	}
}

// RowCount returns the number of data rows.
func (t *Table) RowCount() int {
	return len(t.rows)
}

// Bold returns a bold-formatted string (only in TTY mode).
func Bold(s string) string {
	if !IsTTY() {
		return s
	}
	return color.New(color.Bold).Sprint(s)
}

// Green returns a green-colored string.
func Green(s string) string {
	if !IsTTY() {
		return s
	}
	return color.GreenString(s)
}

// Red returns a red-colored string.
func Red(s string) string {
	if !IsTTY() {
		return s
	}
	return color.RedString(s)
}

// Yellow returns a yellow-colored string.
func Yellow(s string) string {
	if !IsTTY() {
		return s
	}
	return color.YellowString(s)
}

// Cyan returns a cyan-colored string.
func Cyan(s string) string {
	if !IsTTY() {
		return s
	}
	return color.CyanString(s)
}

// Dim returns a dimmed string.
func Dim(s string) string {
	if !IsTTY() {
		return s
	}
	return color.New(color.FgHiBlack).Sprint(s)
}

// Success prints a green checkmark message.
func Success(format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	if IsTTY() {
		fmt.Println(Green("✓") + " " + msg)
	} else {
		fmt.Println(msg)
	}
}

// Error prints an error message to stderr.
func Error(format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	if IsTTY() {
		fmt.Fprintln(os.Stderr, Red("Error:"+" "+msg))
	} else {
		fmt.Fprintln(os.Stderr, "Error: "+msg)
	}
}

// Warn prints a warning message to stderr.
func Warn(format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	if IsTTY() {
		fmt.Fprintln(os.Stderr, Yellow("Warning:"+" "+msg))
	} else {
		fmt.Fprintln(os.Stderr, "Warning: "+msg)
	}
}

// Info prints an info line (only in TTY mode).
func Info(label, value string) {
	if IsTTY() {
		labelColor := color.New(color.FgHiBlack)
		fmt.Printf("  %s  %s\n", labelColor.Sprintf("%-14s", label+":"), value)
	} else {
		fmt.Printf("%s\t%s\n", label, value)
	}
}
