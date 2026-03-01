package output

import (
	"encoding/json"
	"fmt"
	"os"
)

// JSON prints a value as formatted JSON to stdout.
func JSON(v interface{}) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

// JSONCompact prints a value as single-line JSON.
func JSONCompact(v interface{}) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	fmt.Println(string(data))
	return nil
}
