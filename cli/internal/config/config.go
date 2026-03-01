package config

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/viper"
)

// Profile represents a saved server connection.
type Profile struct {
	URL      string `mapstructure:"url"`
	Endpoint string `mapstructure:"endpoint"`
}

// Dir returns the config directory (~/.config/sairo/).
func Dir() string {
	if d := os.Getenv("SAIRO_CONFIG_DIR"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "sairo")
}

// EnsureDir creates the config directory if it doesn't exist.
func EnsureDir() error {
	return os.MkdirAll(Dir(), 0700)
}

// configPath returns the path to config.yaml.
func configPath() string {
	return filepath.Join(Dir(), "config.yaml")
}

// profilesPath returns the path to profiles.yaml.
func profilesPath() string {
	return filepath.Join(Dir(), "profiles.yaml")
}

// Load reads config.yaml into viper.
func Load() {
	viper.SetConfigFile(configPath())
	viper.SetConfigType("yaml")

	// Defaults
	viper.SetDefault("current_profile", "")
	viper.SetDefault("output.format", "table")
	viper.SetDefault("output.color", "auto")
	viper.SetDefault("output.time_format", "relative")

	_ = viper.ReadInConfig() // OK if missing
}

// CurrentProfile returns the active profile name.
func CurrentProfile() string {
	return viper.GetString("current_profile")
}

// SetCurrentProfile sets and persists the active profile.
func SetCurrentProfile(name string) error {
	viper.Set("current_profile", name)
	return save()
}

// save writes the current viper state to config.yaml.
func save() error {
	if err := EnsureDir(); err != nil {
		return err
	}
	return viper.WriteConfigAs(configPath())
}

// LoadProfile reads a specific profile from profiles.yaml.
func LoadProfile(name string) (*Profile, error) {
	v := viper.New()
	v.SetConfigFile(profilesPath())
	v.SetConfigType("yaml")
	if err := v.ReadInConfig(); err != nil {
		return nil, fmt.Errorf("no profiles configured (run: sairo login)")
	}
	var p Profile
	key := "profiles." + name
	if !v.IsSet(key) {
		return nil, fmt.Errorf("profile %q not found", name)
	}
	if err := v.UnmarshalKey(key, &p); err != nil {
		return nil, err
	}
	return &p, nil
}

// SaveProfile writes a profile to profiles.yaml.
func SaveProfile(name string, p Profile) error {
	if err := EnsureDir(); err != nil {
		return err
	}
	v := viper.New()
	v.SetConfigFile(profilesPath())
	v.SetConfigType("yaml")
	_ = v.ReadInConfig() // OK if missing
	v.Set("profiles."+name+".url", p.URL)
	v.Set("profiles."+name+".endpoint", p.Endpoint)
	return v.WriteConfigAs(profilesPath())
}

// DeleteProfile removes a profile from profiles.yaml.
func DeleteProfile(name string) error {
	v := viper.New()
	v.SetConfigFile(profilesPath())
	v.SetConfigType("yaml")
	if err := v.ReadInConfig(); err != nil {
		return nil
	}
	// Viper doesn't support deleting keys, so we rebuild
	all := v.GetStringMap("profiles")
	delete(all, name)
	v2 := viper.New()
	v2.SetConfigFile(profilesPath())
	v2.SetConfigType("yaml")
	for k, val := range all {
		v2.Set("profiles."+k, val)
	}
	return v2.WriteConfigAs(profilesPath())
}

// ListProfiles returns all profile names.
func ListProfiles() []string {
	v := viper.New()
	v.SetConfigFile(profilesPath())
	v.SetConfigType("yaml")
	if err := v.ReadInConfig(); err != nil {
		return nil
	}
	m := v.GetStringMap("profiles")
	names := make([]string, 0, len(m))
	for k := range m {
		names = append(names, k)
	}
	return names
}
