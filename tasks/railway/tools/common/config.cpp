#include "config.h"

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace railtools {

namespace {

std::string trim(const std::string& s) {
  const auto b = s.find_first_not_of(" \t\r");
  if (b == std::string::npos) return "";
  const auto e = s.find_last_not_of(" \t\r");
  return s.substr(b, e - b + 1);
}

}  // namespace

Config Config::load(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot open config " + path);
  std::ostringstream ss;
  ss << in.rdbuf();
  return parse(ss.str(), path);
}

Config Config::parse(const std::string& text, const std::string& origin) {
  Config cfg;
  cfg.origin_ = origin;
  std::istringstream in(text);
  std::string line;
  int lineno = 0;
  while (std::getline(in, line)) {
    ++lineno;
    const auto hash = line.find('#');
    if (hash != std::string::npos) line.resize(hash);
    line = trim(line);
    if (line.empty()) continue;
    const auto eq = line.find('=');
    const std::string where = origin + ":" + std::to_string(lineno);
    if (eq == std::string::npos) throw std::runtime_error(where + ": expected key = value");
    const std::string key = trim(line.substr(0, eq));
    const std::string value = trim(line.substr(eq + 1));
    if (key.empty()) throw std::runtime_error(where + ": empty key");
    size_t pos = 0;
    int64_t v;
    try {
      v = std::stoll(value, &pos);
    } catch (const std::exception&) {
      throw std::runtime_error(where + ": value of " + key + " is not an integer");
    }
    if (pos != value.size()) throw std::runtime_error(where + ": value of " + key + " is not an integer");
    if (!cfg.values_.emplace(key, v).second) throw std::runtime_error(where + ": duplicate key " + key);
  }
  return cfg;
}

void Config::set(const std::string& assignment) {
  const auto eq = assignment.find('=');
  if (eq == std::string::npos) throw std::runtime_error("--set expects key=value, got " + assignment);
  const std::string key = trim(assignment.substr(0, eq));
  if (!values_.count(key)) throw std::runtime_error(origin_ + ": --set of unknown key " + key);
  const Config one = parse(key + " = " + assignment.substr(eq + 1), "--set");
  values_[key] = one.values_.at(key);
}

int64_t Config::get(const std::string& key) {
  const auto it = values_.find(key);
  if (it == values_.end()) throw std::runtime_error(origin_ + ": missing key " + key);
  used_.insert(key);
  return it->second;
}

int64_t Config::get(const std::string& key, int64_t lo, int64_t hi) {
  const int64_t v = get(key);
  if (v < lo || v > hi) {
    throw std::runtime_error(origin_ + ": " + key + " = " + std::to_string(v) + " is outside [" +
                             std::to_string(lo) + ", " + std::to_string(hi) + "]");
  }
  return v;
}

void Config::check_all_used() const {
  for (const auto& [key, value] : values_) {
    if (!used_.count(key)) throw std::runtime_error(origin_ + ": unknown key " + key);
  }
}

}  // namespace railtools
