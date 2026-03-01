package output

import (
	"fmt"
	"math"
	"strings"
	"time"
)

// HumanSize formats bytes into human-readable form.
func HumanSize(bytes int64) string {
	if bytes < 0 {
		return "-"
	}
	const unit = 1024
	if bytes < unit {
		return fmt.Sprintf("%d B", bytes)
	}
	div, exp := int64(unit), 0
	for n := bytes / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	val := float64(bytes) / float64(div)
	suffixes := []string{"KB", "MB", "GB", "TB", "PB"}
	if val >= 100 {
		return fmt.Sprintf("%.0f %s", val, suffixes[exp])
	}
	if val >= 10 {
		return fmt.Sprintf("%.1f %s", val, suffixes[exp])
	}
	return fmt.Sprintf("%.1f %s", val, suffixes[exp])
}

// HumanTime formats a time as a relative duration (e.g., "2h ago").
func HumanTime(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	d := time.Since(t)
	if d < 0 {
		return "just now"
	}
	switch {
	case d < 1*time.Minute:
		return "just now"
	case d < 1*time.Hour:
		m := int(d.Minutes())
		if m == 1 {
			return "1m ago"
		}
		return fmt.Sprintf("%dm ago", m)
	case d < 24*time.Hour:
		h := int(d.Hours())
		if h == 1 {
			return "1h ago"
		}
		return fmt.Sprintf("%dh ago", h)
	case d < 30*24*time.Hour:
		days := int(d.Hours() / 24)
		if days == 1 {
			return "1d ago"
		}
		return fmt.Sprintf("%dd ago", days)
	default:
		months := int(d.Hours() / 24 / 30)
		if months <= 1 {
			return "1mo ago"
		}
		return fmt.Sprintf("%dmo ago", months)
	}
}

// HumanDuration formats a duration for display.
func HumanDuration(d time.Duration) string {
	if d < 1*time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	if d < 1*time.Hour {
		m := int(d.Minutes())
		s := int(d.Seconds()) % 60
		if s == 0 {
			return fmt.Sprintf("%dm", m)
		}
		return fmt.Sprintf("%dm %ds", m, s)
	}
	h := int(d.Hours())
	m := int(d.Minutes()) % 60
	if m == 0 {
		return fmt.Sprintf("%dh", h)
	}
	return fmt.Sprintf("%dh %dm", h, m)
}

// ParseTime attempts to parse various time formats from Sairo API responses.
func ParseTime(s string) (time.Time, error) {
	if s == "" {
		return time.Time{}, nil
	}
	formats := []string{
		time.RFC3339,
		time.RFC3339Nano,
		"2006-01-02T15:04:05",
		"2006-01-02T15:04:05Z",
		"2006-01-02 15:04:05",
	}
	for _, f := range formats {
		if t, err := time.Parse(f, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("cannot parse time: %s", s)
}

// Percentage formats a ratio as a percentage string.
func Percentage(part, total int64) string {
	if total == 0 {
		return "0%"
	}
	pct := float64(part) / float64(total) * 100
	if pct < 0.1 && pct > 0 {
		return "<0.1%"
	}
	return fmt.Sprintf("%.1f%%", pct)
}

// FormatNumber adds commas to integers (e.g., 1234567 → "1,234,567").
func FormatNumber(n int64) string {
	if n < 0 {
		return "-" + FormatNumber(-n)
	}
	s := fmt.Sprintf("%d", n)
	if len(s) <= 3 {
		return s
	}
	var result strings.Builder
	remainder := len(s) % 3
	if remainder > 0 {
		result.WriteString(s[:remainder])
	}
	for i := remainder; i < len(s); i += 3 {
		if result.Len() > 0 {
			result.WriteByte(',')
		}
		result.WriteString(s[i : i+3])
	}
	return result.String()
}

// Speed formats bytes per second.
func Speed(bytesPerSec float64) string {
	if math.IsNaN(bytesPerSec) || math.IsInf(bytesPerSec, 0) || bytesPerSec < 0 {
		return "-"
	}
	return HumanSize(int64(bytesPerSec)) + "/s"
}
