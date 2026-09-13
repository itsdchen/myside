add_pk_library(util basiclib.cc time_utils.cc secmaster.cc funding_master.cc symbolizer.cc supabase_credentials.cc alerts.cc urls.cc)

target_link_libraries(
  util
  PUBLIC pkcore context
  PRIVATE cpr::cpr fmt::fmt nlohmann_json::nlohmann_json
)
