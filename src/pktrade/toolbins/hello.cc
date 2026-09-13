#include <stdio.h>

#include <vector>

#include <fstream>

#include <fmt/format.h>
#include <magic_enum.hpp>

#include "pktrade/enums/basic_enums.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

#include "csv.h"
#include <csv.hpp>

using namespace rapidjson;
using namespace pktrade;
using namespace csv;

// Sanity check the time conversion.
void test_postgres_time_parse() {
  // GOt it by requesting the db.
  std::string postgres_time = "2025-09-15T18:46:18.680045+00:00";

  int64_t postgres_t_epoch = time_utils::postgresTimestampToSecs(postgres_time);
  std::cout << fmt::format("{} -> {}", postgres_time, postgres_t_epoch) << std::endl;
}


void test_email() {
  util::Mailer::instance().send_mail("Hello World", "This is a test message\n Do you read",
                                     "dbkhanh@gmail.com");
  printf("Sending email.\n");
  sleep(5);
}

int main() {
  // test_postgres_time_parse();
  test_email();
  return 0;
}
